"""Economic-calendar providers behind one abstraction, plus the monitor that
turns *upcoming / released* high-impact events into NormalizedEvents.

Live source: the public weekly Forex Factory mirror JSON (no key). It carries
title, currency, time, impact, forecast and previous; ``actual`` is not in this
feed, so live events never claim an actual value.
Replay source: scripted scenario, always labelled replay, through the same path.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta

from pydantic import AwareDatetime, BaseModel

from events.core import EventSource, NormalizedEvent

logger = logging.getLogger("tars.monitors.calendar")

# Which quoted symbols a currency move can affect.
CURRENCY_SYMBOLS = {
    "USD": ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD"),
    "EUR": ("EURUSD",), "GBP": ("GBPUSD",), "JPY": ("USDJPY",), "AUD": ("AUDUSD",),
    "CAD": ("USDCAD",), "CHF": ("USDCHF",), "NZD": ("NZDUSD",),
}
LEAD_MINUTES = {"High": 15, "Medium": 3}
SEVERITY = {"High": 3, "Medium": 2}


class CalendarEvent(BaseModel):
    id: str
    currency: str
    event: str
    timestamp: AwareDatetime
    importance: str
    previous: str | None = None
    forecast: str | None = None
    actual: str | None = None
    replay: bool = False


class EconomicCalendarProvider(ABC):
    name: str
    is_replay = False

    @abstractmethod
    async def fetch(self) -> list[CalendarEvent]: ...


class NewsProvider(ABC):
    """Future headline source. Not wired: no reliable free live headline API was
    adopted for the demo, and TARS does not scrape multiple sites."""
    name: str

    @abstractmethod
    async def latest(self, symbols: list[str]) -> list[dict]: ...


class FaireconomyCalendarProvider(EconomicCalendarProvider):
    name = "live:faireconomy-weekly"
    URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

    async def fetch(self) -> list[CalendarEvent]:
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(self.URL)
            response.raise_for_status()
        return self.parse(response.json())

    @staticmethod
    def parse(rows: list[dict]) -> list[CalendarEvent]:
        events = []
        for row in rows:
            try:
                when = datetime.fromisoformat(row["date"]).astimezone(UTC)
                key = f'{row["country"]}|{row["title"]}|{when.isoformat()}'
                events.append(CalendarEvent(
                    id=hashlib.sha1(key.encode()).hexdigest()[:16], currency=row["country"],
                    event=row["title"], timestamp=when, importance=row.get("impact", "Low"),
                    previous=row.get("previous") or None, forecast=row.get("forecast") or None))
            except Exception:
                continue
        return events


class ReplayCalendarProvider(EconomicCalendarProvider):
    """Scripted NFP scenario, relative to ``started``. Clearly not live data."""
    name = "replay"
    is_replay = True

    def __init__(self, started: datetime | None = None):
        self.started = started or datetime.now(UTC)

    async def fetch(self) -> list[CalendarEvent]:
        return [CalendarEvent(
            id="replay-nfp", currency="USD", event="Non-Farm Employment Change",
            timestamp=self.started + timedelta(minutes=5), importance="High",
            previous="142K", forecast="180K", actual=None, replay=True)]


class CalendarMonitor:
    def __init__(self, provider: EconomicCalendarProvider, publish, on_state, symbols: list[str], *,
                 refresh_seconds: float = 1800, tick_seconds: float = 20, now=lambda: datetime.now(UTC)):
        self.provider, self.publish, self.on_state = provider, publish, on_state
        self.symbols = [s.upper() for s in symbols]
        self.refresh_seconds, self.tick_seconds, self.now = refresh_seconds, tick_seconds, now
        self.events: list[CalendarEvent] = []
        self.state, self.detail = "DISCONNECTED", "not started"
        self.fetched_at: datetime | None = None
        self._task: asyncio.Task | None = None

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("calendar tick failed; monitor remains alive")
            await asyncio.sleep(self.tick_seconds)

    async def refresh(self):
        try:
            self.events = await self.provider.fetch()
            self.fetched_at = self.now()
            self.state, self.detail = "CONNECTED", f"{len(self.events)} events ({self.provider.name})"
        except Exception as exc:
            self.state, self.detail = "ERROR", f"calendar fetch failed: {type(exc).__name__}"
        await self.on_state(self.state)

    async def tick(self):
        if self.fetched_at is None or (self.now() - self.fetched_at).total_seconds() >= self.refresh_seconds:
            await self.refresh()
        now = self.now()
        for event in self.events:
            lead = LEAD_MINUTES.get(event.importance)
            if lead is None:
                continue
            delta = (event.timestamp - now).total_seconds() / 60
            affected = [s for s in CURRENCY_SYMBOLS.get(event.currency, ()) if not self.symbols or s in self.symbols]
            if self.symbols and not affected:
                continue
            if 0 < delta <= lead:
                await self.publish(self._event(event, affected, "calendar.upcoming",
                                               f"{event.currency} {event.event} in {int(delta) + 1} min"))
            elif -2 <= delta <= 0 and event.actual is not None:
                await self.publish(self._event(event, affected, "calendar.released",
                                               f"{event.currency} {event.event} released: {event.actual}"))

    def _event(self, event: CalendarEvent, affected: list[str], kind: str, title: str) -> NormalizedEvent:
        tag = "[DEMO REPLAY] " if event.replay else ""
        details = ", ".join(f"{k} {v}" for k, v in (("previous", event.previous), ("forecast", event.forecast),
                                                    ("actual", event.actual)) if v)
        return NormalizedEvent(
            source=EventSource.ECONOMIC_CALENDAR, kind=kind, severity=SEVERITY[event.importance],
            symbol=affected[0] if affected else None, title=(tag + title)[:200],
            summary=(tag + (f"{details}. " if details else "") + (f"Affects {', '.join(affected)}." if affected else ""))[:2000],
            dedupe_key=f"{event.id}:{kind}", expires_at=event.timestamp + timedelta(minutes=10),
            payload={"currency": event.currency, "event": event.event, "importance": event.importance,
                     "previous": event.previous, "forecast": event.forecast, "actual": event.actual,
                     "at": event.timestamp.isoformat(), "affected": affected, "replay": event.replay})

    def next_event(self) -> dict | None:
        now = self.now()
        upcoming = sorted((e for e in self.events if e.timestamp > now and e.importance in LEAD_MINUTES),
                          key=lambda e: e.timestamp)
        if not upcoming:
            return None
        e = upcoming[0]
        return {"currency": e.currency, "event": e.event, "at": e.timestamp.isoformat(),
                "importance": e.importance, "replay": e.replay}

    def snapshot(self) -> dict:
        return {"state": self.state, "detail": self.detail, "source": self.provider.name,
                "replay": self.provider.is_replay, "next": self.next_event()}
