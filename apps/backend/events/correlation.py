"""CorrelationEngine -- a calendar event, an open MT5 position, and an actively-watched
TradingView chart on the SAME symbol are one story, not three unrelated notifications (mission
item 14's own example: CPI in 8 minutes + an open EURUSD position + EURUSD active on TradingView
-> one correlated alert).

Listens to the existing `RealtimeEventCore` broadcast stream (`core.subscribe`) rather than
building a second event pipeline, and when a calendar event fires for a symbol the user also has
exposure to and is actively looking at, publishes ONE additional, higher-severity synthesized
event through the SAME `core.publish()`. It never suppresses the individual per-source events --
`RealtimeEventCore`'s own design principle ("Notify before optional reasoning; provider outage
cannot suppress alerts") applies here too: this layer only ever adds a clearer signal on top of
the honest per-source ones, never replaces them.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from events.core import EventSource, NormalizedEvent

logger = logging.getLogger("tars.events.correlation")

_CALENDAR_KINDS = {"calendar.upcoming", "calendar.released"}


class CorrelationEngine:
    def __init__(self, core, monitors):
        self.core, self.monitors = core, monitors
        self._unsubscribe = None

    def start(self) -> None:
        self._unsubscribe = self.core.subscribe(self._on_event)

    def stop(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None

    async def _on_event(self, payload: dict) -> None:
        if payload.get("type") != "proactive_event":
            return
        event = payload.get("event") or {}
        if event.get("source") != EventSource.ECONOMIC_CALENDAR.value or event.get("kind") not in _CALENDAR_KINDS:
            return
        symbol = event.get("symbol")
        if not symbol:
            return
        if not self._has_open_position(symbol) or not await self._is_watching(symbol):
            return

        currency = (event.get("payload") or {}).get("currency") or symbol
        title = event.get("title", "")
        await self.core.publish(NormalizedEvent(
            source=EventSource.SYSTEM, kind="correlated_alert", severity=4, symbol=symbol,
            title=f"{symbol}: {title} -- you hold a position and are watching it"[:200],
            summary=(f"{title} for {currency}. You have an open MT5 position on {symbol} and are "
                    f"actively watching it on TradingView right now -- this event is directly relevant "
                    f"to what you're exposed to."),
            # Keyed on the triggering event's own STABLE dedupe_key (survives across the calendar
            # monitor's repeated ticks for the same calendar entry), not its `id` (a fresh random
            # UUID every publish) -- using `id` would defeat RealtimeEventCore's own cooldown and
            # re-fire a "new" correlated alert every ~30s for the entire 15-minute lead window
            # instead of once per underlying event, same as any other source's alert.
            dedupe_key=f"correlated:{event.get('dedupe_key') or event.get('id')}",
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
            payload={"triggering_event_id": event.get("id"), "symbol": symbol,
                    "has_mt5_position": True, "tradingview_watching": True},
        ))
        logger.info("[correlation] correlated alert for %s: calendar event + open MT5 position + TradingView watch", symbol)

    def _has_open_position(self, symbol: str) -> bool:
        mt5 = getattr(self.monitors, "mt5", None)
        if mt5 is None:
            return False
        try:
            positions: list[dict[str, Any]] = mt5.snapshot().get("positions") or []
        except Exception:
            return False
        return any(p.get("symbol") == symbol for p in positions)

    async def _is_watching(self, symbol: str) -> bool:
        store = getattr(self.monitors, "hot_chart_store", None)
        if store is None:
            return False
        try:
            latest = await store.get_latest()
        except Exception:
            return False
        if latest is None or latest.freshness().value not in ("hot", "warm"):
            return False
        return (latest.identity.symbol or "").upper() == symbol.upper()
