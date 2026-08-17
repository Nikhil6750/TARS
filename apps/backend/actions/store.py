from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import aiosqlite

from actions.errors import DuplicateActionError
from app.action_contracts import ActionRequest, ActionResult, ActionStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS action_requests (
    request_id TEXT PRIMARY KEY,
    request_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    status TEXT NOT NULL,
    risk_level TEXT,
    confirmation_token_hash TEXT,
    confirmation_expires_at TEXT,
    confirmation_consumed INTEGER NOT NULL DEFAULT 0 CHECK (confirmation_consumed IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    event TEXT NOT NULL,
    status TEXT NOT NULL,
    risk_level TEXT,
    summary TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (request_id) REFERENCES action_requests(request_id)
);

CREATE INDEX IF NOT EXISTS idx_action_audit_request_sequence
ON action_audit(request_id, sequence);
"""


class ActionStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def initialize(self) -> None:
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def insert_request(
        self, request: ActionRequest, pending: ActionResult, now: datetime
    ) -> None:
        payload = json.dumps(request.to_contract_dict(), separators=(",", ":"))
        result = json.dumps(pending.to_contract_dict(), separators=(",", ":"))
        try:
            await self._conn.execute(
                """
                INSERT INTO action_requests (
                    request_id, request_json, result_json, status, risk_level,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(request.id),
                    payload,
                    result,
                    pending.status.value,
                    pending.risk_level.value if pending.risk_level else None,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            await self._conn.commit()
        except aiosqlite.IntegrityError as exc:
            await self._conn.rollback()
            raise DuplicateActionError(f"Action request {request.id} already exists") from exc

    async def set_result(
        self,
        result: ActionResult,
        now: datetime,
        *,
        confirmation_token_hash: str | None = None,
        confirmation_expires_at: datetime | None = None,
        consume_confirmation: bool = False,
    ) -> None:
        await self._conn.execute(
            """
            UPDATE action_requests
            SET result_json = ?, status = ?, risk_level = ?,
                confirmation_token_hash = COALESCE(?, confirmation_token_hash),
                confirmation_expires_at = COALESCE(?, confirmation_expires_at),
                confirmation_consumed = CASE WHEN ? THEN 1 ELSE confirmation_consumed END,
                updated_at = ?
            WHERE request_id = ?
            """,
            (
                json.dumps(result.to_contract_dict(), separators=(",", ":")),
                result.status.value,
                result.risk_level.value if result.risk_level else None,
                confirmation_token_hash,
                confirmation_expires_at.isoformat() if confirmation_expires_at else None,
                int(consume_confirmation),
                now.isoformat(),
                str(result.request_id),
            ),
        )
        await self._conn.commit()

    async def append_audit(
        self,
        request_id: UUID,
        event: str,
        result: ActionResult,
        now: datetime,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        await self._conn.execute(
            """
            INSERT INTO action_audit (
                request_id, event, status, risk_level, summary, details_json, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(request_id),
                event,
                result.status.value,
                result.risk_level.value if result.risk_level else None,
                result.summary,
                json.dumps(details or {}, separators=(",", ":"), default=str),
                now.isoformat(),
            ),
        )
        await self._conn.commit()

    async def get_record(self, request_id: UUID | str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM action_requests WHERE request_id = ?", (str(request_id),)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_result(self, request_id: UUID | str) -> ActionResult | None:
        record = await self.get_record(request_id)
        if record is None:
            return None
        return ActionResult.model_validate(json.loads(record["result_json"]))

    async def get_request(self, request_id: UUID | str) -> ActionRequest | None:
        record = await self.get_record(request_id)
        if record is None:
            return None
        return ActionRequest.model_validate(json.loads(record["request_json"]))

    async def mark_confirmation_consumed(self, request_id: UUID, now: datetime) -> bool:
        cursor = await self._conn.execute(
            """
            UPDATE action_requests
            SET confirmation_consumed = 1, updated_at = ?
            WHERE request_id = ? AND status = ? AND confirmation_consumed = 0
            """,
            (now.isoformat(), str(request_id), ActionStatus.CONFIRMATION_REQUIRED.value),
        )
        await self._conn.commit()
        return cursor.rowcount == 1

    async def list_audit(self, request_id: UUID | str) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            """
            SELECT sequence, request_id, event, status, risk_level, summary,
                   details_json, recorded_at
            FROM action_audit WHERE request_id = ? ORDER BY sequence ASC
            """,
            (str(request_id),),
        )
        rows = await cursor.fetchall()
        return [
            {
                "sequence": row["sequence"],
                "request_id": row["request_id"],
                "event": row["event"],
                "status": row["status"],
                "risk_level": row["risk_level"],
                "summary": row["summary"],
                "details": json.loads(row["details_json"]),
                "recorded_at": row["recorded_at"],
            }
            for row in rows
        ]

    async def list_recent_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            """
            SELECT sequence, request_id, event, status, risk_level, summary,
                   details_json, recorded_at
            FROM action_audit ORDER BY sequence DESC LIMIT ?
            """,
            (max(1, min(limit, 500)),),
        )
        rows = await cursor.fetchall()
        return [
            {
                "sequence": row["sequence"],
                "request_id": row["request_id"],
                "event": row["event"],
                "status": row["status"],
                "risk_level": row["risk_level"],
                "summary": row["summary"],
                "details": json.loads(row["details_json"]),
                "recorded_at": row["recorded_at"],
            }
            for row in rows
        ]
