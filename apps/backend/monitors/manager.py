"""Owns the local monitors and reports truthful per-source status.

MT5 / calendar / TradingView state is derived from what each source is actually
doing right now; nothing here fabricates a CONNECTED badge. The demo replay uses
the same event core and is labelled replay in status, titles and payloads.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from events.core import EventSource, NormalizedEvent, ProviderStatus
from monitors.calendar import (
    CalendarMonitor,
    EconomicCalendarProvider,
    FaireconomyCalendarProvider,
    ReplayCalendarProvider,
)
from monitors.mt5_provider import MT5Provider, MT5State

logger = logging.getLogger("tars.monitors")


class MonitorManager:
    def __init__(self, core, hot_chart_store, *, mt5_enabled=True, calendar_enabled=True,
                 symbols: list[str] | None = None, mt5_loader=None,
                 calendar_provider: EconomicCalendarProvider | None = None):
        self.core, self.hot_chart_store = core, hot_chart_store
        self.symbols = symbols or ["EURUSD", "XAUUSD"]
        kwargs = {"module_loader": mt5_loader} if mt5_loader else {}
        self.mt5 = MT5Provider(self.symbols, core.publish, self._mt5_state, **kwargs) if mt5_enabled else None
        self.calendar = CalendarMonitor(calendar_provider or FaireconomyCalendarProvider(), core.publish,
                                        self._calendar_state, self.symbols) if calendar_enabled else None
        self.replay_calendar: CalendarMonitor | None = None
        self.replay_quote: dict | None = None
        self._replay_task: asyncio.Task | None = None

    async def start(self):
        for monitor in (self.mt5, self.calendar):
            if monitor:
                await monitor.start()

    async def stop(self):
        await self.stop_replay()
        for monitor in (self.mt5, self.calendar):
            if monitor:
                await monitor.stop()

    async def _mt5_state(self, state: MT5State):
        await self.core.set_provider_status(EventSource.MT5, {
            MT5State.CONNECTED: ProviderStatus.CONNECTED, MT5State.ERROR: ProviderStatus.ERROR,
        }.get(state, ProviderStatus.DISCONNECTED))

    async def _calendar_state(self, state: str):
        await self.core.set_provider_status(EventSource.ECONOMIC_CALENDAR, {
            "CONNECTED": ProviderStatus.CONNECTED, "ERROR": ProviderStatus.ERROR,
        }.get(state, ProviderStatus.DISCONNECTED))

    # ---- TradingView (existing chart-window monitor) --------------------
    async def tradingview_status(self) -> dict:
        try:
            latest = await self.hot_chart_store.get_latest()
        except Exception as exc:
            return {"state": "ERROR", "detail": type(exc).__name__}
        if latest is None:
            return {"state": "NOT FOUND", "detail": "No chart window observed yet"}
        freshness = latest.freshness().value
        age = round(latest.age_ms() / 1000)
        ident = latest.identity
        label = f"{getattr(ident, 'symbol', None) or 'chart'} {getattr(ident, 'timeframe', None) or ''}".strip()
        if freshness in {"hot", "warm"}:
            return {"state": "MONITORING", "detail": f"{label}, analysed {age}s ago", "freshness": freshness}
        return {"state": "NOT FOUND", "detail": f"Last chart frame {age}s ago ({label})", "freshness": freshness}

    # ---- status ----------------------------------------------------------
    async def status(self) -> dict:
        mt5 = self.mt5.snapshot() if self.mt5 else {"state": "DISABLED", "detail": "MT5 monitor disabled"}
        calendar = (self.replay_calendar or self.calendar)
        cal = calendar.snapshot() if calendar else {"state": "DISABLED", "detail": "calendar disabled"}
        quote = None
        symbol = self.symbols[0]
        if self.replay_quote:
            quote = {**self.replay_quote, "source": "DEMO REPLAY"}
        elif mt5["state"] == MT5State.CONNECTED.value and symbol in mt5["quotes"]:
            quote = {"symbol": symbol, **mt5["quotes"][symbol], "source": "MT5"}
        return {"mt5": mt5, "tradingview": await self.tradingview_status(), "calendar": cal,
                "news": {"state": "NOT CONFIGURED", "detail": "No live headline provider configured"},
                "quote": quote, "replay": self.replay_quote is not None or self._replay_task is not None and not self._replay_task.done(),
                "providers": self.core.providers}

    # ---- DEMO REPLAY (real pipeline, labelled) --------------------------
    async def start_replay(self) -> dict:
        await self.stop_replay()
        self._replay_task = asyncio.create_task(self._run_replay())
        return {"started": True, "mode": "DEMO REPLAY"}

    async def stop_replay(self):
        if self._replay_task:
            self._replay_task.cancel()
            await asyncio.gather(self._replay_task, return_exceptions=True)
            self._replay_task = None
        if self.replay_calendar:
            await self.replay_calendar.stop()
            self.replay_calendar = None
        self.replay_quote = None

    async def _run_replay(self):
        run = uuid_suffix()
        try:
            self.replay_quote = {"symbol": "EURUSD", "bid": 1.08420, "ask": 1.08422, "spread": 2.0}
            # 1. high-impact USD event approaching, via the real calendar monitor path
            self.replay_calendar = CalendarMonitor(
                ReplayCalendarProvider(), self.core.publish, self._noop_state, self.symbols, tick_seconds=1)
            self.replay_calendar.events = await self.replay_calendar.provider.fetch()
            self.replay_calendar.fetched_at = datetime.now(UTC)
            self.replay_calendar.state, self.replay_calendar.detail = "CONNECTED", "replay scenario"
            for event in self.replay_calendar.events:
                event.id = f"replay-nfp-{run}"
            await self.replay_calendar.tick()
            await asyncio.sleep(10)
            # 2. release: actual far above forecast
            await self._publish_replay(
                "calendar.released", 3, "USD Non-Farm Employment Change released: 254K",
                "actual 254K, forecast 180K, previous 142K. Strong beat; USD-positive. Affects EURUSD.",
                f"{run}:released", {"currency": "USD", "actual": "254K", "forecast": "180K", "previous": "142K"},
                EventSource.ECONOMIC_CALENDAR)
            # 3. simulated market reaction
            for i in range(9):
                bid = round(1.08420 - 0.00034 * (i + 1), 5)
                self.replay_quote = {"symbol": "EURUSD", "bid": bid, "ask": round(bid + 0.00002, 5), "spread": 2.0}
                await asyncio.sleep(1)
            await self._publish_replay(
                "price.move", 3, "EURUSD down 0.28% in 9s", "EURUSD fell from 1.08420 to 1.08114 after the payrolls beat.",
                f"{run}:move", {"move_pct": -0.28, "from": 1.08420, "to": 1.08114}, EventSource.MT5)
            await asyncio.sleep(60)
            self.replay_quote = None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("demo replay failed")

    async def _noop_state(self, state: str):
        return None

    async def _publish_replay(self, kind, severity, title, summary, dedupe, payload, source):
        await self.core.publish(NormalizedEvent(
            source=source, kind=kind, severity=severity, symbol="EURUSD", title="[DEMO REPLAY] " + title,
            summary="[DEMO REPLAY] " + summary, dedupe_key=dedupe, payload={**payload, "replay": True},
            expires_at=datetime.now(UTC) + timedelta(minutes=10)))


def uuid_suffix() -> str:
    from uuid import uuid4
    return uuid4().hex[:8]
