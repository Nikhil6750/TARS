"""Builds a `TradingContext` from the deterministic sources of truth —
`EventService` (active setups / recent warnings, same as
`assistant/grounding.py` uses) and a `StrategyProvider`. The one place that
assembles trading grounding for skills/agents, so they don't each reach into
both dependencies independently and risk drifting from what the ordinary
assistant router already grounds on.
"""
from __future__ import annotations

from datetime import UTC, datetime

from events.service import EventService
from trading.models import TradingContext
from trading.provider import StrategyProvider


class TradingContextBuilder:
    def __init__(self, event_service: EventService, strategy_provider: StrategyProvider):
        self._events = event_service
        self._strategy = strategy_provider

    async def build(self, *, warnings_limit: int = 5) -> TradingContext:
        active = await self._events.get_active_setups()
        warnings = await self._events.get_recent_warnings(limit=warnings_limit)
        status = await self._strategy.status()
        strategy = await self._strategy.get_strategy() if status.value == "CONFIGURED" else None
        return TradingContext(
            strategy_status=status,
            active_setups=active,
            recent_warnings=warnings,
            strategy=strategy,
            generated_at=datetime.now(UTC),
        )
