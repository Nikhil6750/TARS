"""Event persistence + deterministic active-state calculation.

"Active state" answers: for a given symbol, is there a setup a client
should currently show as live? The rule is intentionally simple and fully
deterministic — no model call, no heuristic scoring, per ARCHITECTURE.md's
"trading facts always come from deterministic state, never model
invention":

- `state in {SETUP_DEVELOPING, SETUP_VALID}` and
  `validation_status in {PENDING, VALID}` => symbol's active setup is this
  event (upsert, replacing whatever was previously active for that symbol).
- `state in {IDLE, SETUP_INVALIDATED}` or `validation_status in
  {INVALID, EXPIRED}` => symbol's active setup is cleared (invalidated).
- `RISK_WARNING` / `SYSTEM_WARNING` are informational broadcasts; they are
  persisted and broadcast but never replace or clear a symbol's active
  setup by themselves (a system warning about connectivity, say, is not a
  statement about whether XAUUSD's setup is still valid).
- An active setup past its own `expires_at` is treated as invalidated the
  next time it is read, even with no new event (see `get_active_setups`).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from app.schemas import ACTIVE_STATES, EventState, TradingEvent, ValidationStatus

CLEARING_STATES = {EventState.IDLE, EventState.SETUP_INVALIDATED}
CLEARING_VALIDATION_STATUSES = {ValidationStatus.INVALID, ValidationStatus.EXPIRED}
INFORMATIONAL_STATES = {EventState.RISK_WARNING, EventState.SYSTEM_WARNING}


@dataclass
class ActiveStateChange:
    symbol: str
    action: str  # "upserted" | "cleared" | "unchanged"


class EventService:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    async def record_event(self, event: TradingEvent) -> ActiveStateChange:
        await self._persist(event)
        return await self._apply_active_state(event)

    async def _persist(self, event: TradingEvent) -> None:
        await self._conn.execute(
            """
            INSERT INTO trading_events (
                event_id, schema_version, timestamp, source, symbol,
                strategy_id, state, direction, entry, stop_loss,
                take_profit, risk_reward, risk_percent, validation_status,
                reason_codes, warnings, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(event.event_id),
                event.schema_version,
                event.timestamp.isoformat(),
                event.source.value,
                event.symbol,
                event.strategy_id,
                event.state.value,
                event.direction.value if event.direction else None,
                event.entry,
                event.stop_loss,
                event.take_profit,
                event.risk_reward,
                event.risk_percent,
                event.validation_status.value,
                json.dumps(event.reason_codes),
                json.dumps(event.warnings),
                event.expires_at.isoformat() if event.expires_at else None,
            ),
        )
        await self._conn.commit()

    async def _apply_active_state(self, event: TradingEvent) -> ActiveStateChange:
        if event.state in INFORMATIONAL_STATES:
            return ActiveStateChange(symbol=event.symbol, action="unchanged")

        should_clear = (
            event.state in CLEARING_STATES
            or event.validation_status in CLEARING_VALIDATION_STATUSES
        )
        if should_clear:
            await self._conn.execute(
                "DELETE FROM active_setups WHERE symbol = ?", (event.symbol,)
            )
            await self._conn.commit()
            return ActiveStateChange(symbol=event.symbol, action="cleared")

        if event.state in ACTIVE_STATES:
            now = datetime.now(UTC).isoformat()
            await self._conn.execute(
                """
                INSERT INTO active_setups (symbol, event_id, state, validation_status, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    event_id = excluded.event_id,
                    state = excluded.state,
                    validation_status = excluded.validation_status,
                    updated_at = excluded.updated_at
                """,
                (event.symbol, str(event.event_id), event.state.value, event.validation_status.value, now),
            )
            await self._conn.commit()
            return ActiveStateChange(symbol=event.symbol, action="upserted")

        return ActiveStateChange(symbol=event.symbol, action="unchanged")

    async def get_history(
        self, symbol: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        if symbol:
            cursor = await self._conn.execute(
                """
                SELECT * FROM trading_events WHERE symbol = ?
                ORDER BY received_at DESC LIMIT ?
                """,
                (symbol.upper(), limit),
            )
        else:
            cursor = await self._conn.execute(
                "SELECT * FROM trading_events ORDER BY received_at DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [_row_to_event_dict(row) for row in rows]

    async def get_recent_warnings(self, limit: int = 10) -> list[dict[str, Any]]:
        """Most recent RISK_WARNING/SYSTEM_WARNING events — informational
        broadcasts that never touch active_setups (see INFORMATIONAL_STATES
        above), so they need their own query rather than a JOIN."""
        limit = max(1, min(limit, 100))
        cursor = await self._conn.execute(
            """
            SELECT * FROM trading_events
            WHERE state IN ('RISK_WARNING', 'SYSTEM_WARNING')
            ORDER BY received_at DESC LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [_row_to_event_dict(row) for row in rows]

    async def get_active_setups(self) -> list[dict[str, Any]]:
        """Returns current active setups, lazily invalidating any that have
        passed their event's `expires_at` without a new event arriving."""
        cursor = await self._conn.execute(
            """
            SELECT a.symbol AS symbol, e.*
            FROM active_setups a
            JOIN trading_events e ON e.event_id = a.event_id
            ORDER BY a.updated_at DESC
            """
        )
        rows = await cursor.fetchall()
        now = datetime.now(UTC)
        live: list[dict[str, Any]] = []
        expired_symbols: list[str] = []
        for row in rows:
            expires_at = row["expires_at"]
            if expires_at and datetime.fromisoformat(expires_at) < now:
                expired_symbols.append(row["symbol"])
                continue
            live.append(_row_to_event_dict(row))
        if expired_symbols:
            await self._conn.executemany(
                "DELETE FROM active_setups WHERE symbol = ?",
                [(s,) for s in expired_symbols],
            )
            await self._conn.commit()
        return live


def _row_to_event_dict(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "schema_version": row["schema_version"],
        "event_id": row["event_id"],
        "timestamp": row["timestamp"],
        "source": row["source"],
        "symbol": row["symbol"],
        "strategy_id": row["strategy_id"],
        "state": row["state"],
        "direction": row["direction"],
        "entry": row["entry"],
        "stop_loss": row["stop_loss"],
        "take_profit": row["take_profit"],
        "risk_reward": row["risk_reward"],
        "risk_percent": row["risk_percent"],
        "validation_status": row["validation_status"],
        "reason_codes": json.loads(row["reason_codes"]),
        "warnings": json.loads(row["warnings"]),
        "expires_at": row["expires_at"],
    }
