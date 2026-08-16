"""Mock trading-event generator — the V1 stand-in for quant_brain.

Emits only events valid against contracts/trading-event.schema.json, with
`source: "mock"`. It never invents an AI confidence score (the contract has
no such field) and never claims a validated outcome — reason_codes/warnings
here are plainly mock-generator bookkeeping, not simulated quant results.
Each symbol cycles through a simple, deterministic lifecycle so consumers
can be exercised against every contract state without special-casing.
"""
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.schemas import Direction, EventSource, EventState, TradingEvent, ValidationStatus

logger = logging.getLogger("tars.mock_generator")

# (symbol, indicative reference price) — arbitrary but stable seeds, not
# live market data. The generator's job is contract-shaped traffic, not
# price realism.
SYMBOLS = [
    ("XAUUSD", 2400.0),
    ("ES", 5300.0),
    ("EURUSD", 1.085),
]

_LIFECYCLE = [
    EventState.SETUP_DEVELOPING,
    EventState.SETUP_VALID,
    EventState.SETUP_INVALIDATED,
    EventState.IDLE,
]


@dataclass
class _SymbolCursor:
    symbol: str
    base_price: float
    step: int = 0
    direction: Direction = field(default=Direction.LONG)


class MockEventGenerator:
    def __init__(
        self,
        emit: Callable[[TradingEvent], Awaitable[None]],
        interval_seconds: float = 45.0,
        rng: random.Random | None = None,
    ):
        self._emit = emit
        self._interval = interval_seconds
        self._rng = rng or random.Random()
        self._cursors = [_SymbolCursor(symbol=s, base_price=p) for s, p in SYMBOLS]
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        try:
            while True:
                cursor = self._rng.choice(self._cursors)
                event = self._next_event(cursor)
                await self._emit(event)
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("mock event generator crashed")
            raise

    def _next_event(self, cursor: _SymbolCursor) -> TradingEvent:
        # Small chance of an informational event instead of advancing the
        # symbol's setup lifecycle.
        if self._rng.random() < 0.08:
            return self._informational_event(cursor)

        state = _LIFECYCLE[cursor.step % len(_LIFECYCLE)]
        cursor.step += 1

        if state == EventState.SETUP_DEVELOPING:
            cursor.direction = self._rng.choice([Direction.LONG, Direction.SHORT])
            return self._setup_event(cursor, state, ValidationStatus.PENDING)
        if state == EventState.SETUP_VALID:
            return self._setup_event(cursor, state, ValidationStatus.VALID)
        if state == EventState.SETUP_INVALIDATED:
            return self._setup_event(
                cursor,
                state,
                ValidationStatus.INVALID,
                reason_codes=["MOCK_INVALIDATION"],
            )
        return TradingEvent(
            source=EventSource.mock,
            symbol=cursor.symbol,
            state=EventState.IDLE,
            direction=None,
            validation_status=ValidationStatus.EXPIRED,
            reason_codes=["MOCK_CYCLE_RESET"],
        )

    def _setup_event(
        self,
        cursor: _SymbolCursor,
        state: EventState,
        validation_status: ValidationStatus,
        reason_codes: list[str] | None = None,
    ) -> TradingEvent:
        offset = cursor.base_price * 0.004
        sign = 1 if cursor.direction == Direction.LONG else -1
        entry = round(cursor.base_price + self._rng.uniform(-offset, offset), 5)
        stop_loss = round(entry - sign * offset, 5)
        take_profit = round(entry + sign * offset * 2, 5)
        risk = abs(entry - stop_loss)
        reward = abs(take_profit - entry)
        risk_reward = round(reward / risk, 2) if risk else None
        return TradingEvent(
            source=EventSource.mock,
            symbol=cursor.symbol,
            strategy_id="mock-lifecycle-v1",
            state=state,
            direction=cursor.direction,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            risk_reward=risk_reward,
            risk_percent=1.0,
            validation_status=validation_status,
            reason_codes=reason_codes or [],
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )

    def _informational_event(self, cursor: _SymbolCursor) -> TradingEvent:
        state = self._rng.choice([EventState.RISK_WARNING, EventState.SYSTEM_WARNING])
        warnings = (
            ["Elevated volatility on mock feed"]
            if state == EventState.RISK_WARNING
            else ["Mock generator heartbeat"]
        )
        return TradingEvent(
            source=EventSource.mock,
            symbol=cursor.symbol,
            state=state,
            direction=None,
            validation_status=ValidationStatus.PENDING,
            warnings=warnings,
            reason_codes=["MOCK_INFORMATIONAL"],
        )
