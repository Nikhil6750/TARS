"""The bridge from an authenticated MT5 session to the market service.

This uses the existing read-only `monitors.mt5_provider.MT5Provider`; it opens
no second connection and has no order path. It exists because a quote stream
alone must not produce LIVE. Every panel is computed from completed bars, so
the status is only honest once those bars come from the feed too. The bridge
keeps both current and tells the service exactly how far each one has got.

Three facts about MT5 data shape this module:

**Timestamps are broker server time, not UTC.** A tick stamped 14:00 at a UTC+3
broker happened at 11:00 UTC. So freshness is never read from the broker's
clock: it is measured by when *we* saw a tick change. The broker's offset is
estimated from those same changing ticks, and only used to place bars.

**An unchanged quote is not a live quote.** A terminal that has lost its server
keeps returning its last tick, so only a change counts, and the first quote
after a start proves nothing.

**The newest rate is still forming.** It is dropped, and the broker's H4 and D1
bars are not used at all: they are cut on server-time boundaries, while the
recorded store is cut on UTC ones. Both are rebuilt here from H1 instead.

Prices become mids, half the spread above the broker's bid-based rates, so that
live bars share the recorded store's basis.
"""
from __future__ import annotations

import logging
import os
import statistics
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from market import universe
from market.store import Candle

logger = logging.getLogger("tars.market.feed")

POLL_SECONDS = 2.0
BARS_REFRESH_SECONDS = 15.0
# Enough history for the rolling windows: M5 and M15 cover the X-Ray and the
# strength windows, H1 also supplies the H4 and D1 bars built here.
RATE_COUNTS = {"M5": 1500, "M15": 1500, "H1": 3000}
# H4 and D1 are rebuilt from H1, so H1 must always cover the longest of them.
_MIN_HOURLY_BARS = 26
# A tick more than this far ahead of our clock, once the broker's offset is
# removed, is a bad timestamp and not a very fresh tick.
MAX_FUTURE_SECONDS = 120.0
# Broker offsets are whole quarter hours, and no real one exceeds this.
_OFFSET_STEP_SECONDS = 900
_MAX_OFFSET_SECONDS = 14 * 3600


class FeedConnection(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    DISCONNECTED = "DISCONNECTED"
    CONNECTED = "CONNECTED"


def connection_from_provider(state: str) -> FeedConnection:
    """The provider's states, in the terms the dashboard reports."""
    return {
        "CONNECTED": FeedConnection.CONNECTED,
        "DISCONNECTED_AUTH_REQUIRED": FeedConnection.AUTH_REQUIRED,
        "NOT INSTALLED": FeedConnection.NOT_CONFIGURED,
        "DISABLED": FeedConnection.NOT_CONFIGURED,
    }.get(state, FeedConnection.DISCONNECTED)


def estimate_server_offset(tick_epoch: float, received_epoch: float) -> int | None:
    """The broker's offset from UTC in seconds, from a tick seen just now.

    Rounds to a quarter hour, which absorbs network and polling delay. None if
    the result is not a plausible timezone, which means the timestamp is bad.
    """
    offset = round((tick_epoch - received_epoch) / _OFFSET_STEP_SECONDS) * _OFFSET_STEP_SECONDS
    return offset if abs(offset) <= _MAX_OFFSET_SECONDS else None


def quote_problem(quote: dict) -> str | None:
    """Why a quote cannot be believed, or None."""
    bid, ask, stamp = quote.get("bid"), quote.get("ask"), quote.get("time")
    if not bid or not ask or bid <= 0 or ask <= 0:
        return "non-positive price"
    if ask < bid:
        return "crossed quote"
    if not stamp:
        return "no timestamp"
    return None


class TickTracker:
    """Decides whether quotes are really moving. Pure: every clock reading is
    passed in, so each transition can be tested without a broker."""

    def __init__(self) -> None:
        self._last_seen: dict[str, tuple[float, float, float]] = {}
        self.last_change: datetime | None = None
        self.last_change_by_symbol: dict[str, datetime] = {}
        self.server_offset: int | None = None
        self.rejected: dict[str, str] = {}

    def observe(self, quotes: dict[str, dict], received_at: datetime) -> None:
        offsets: list[int] = []
        self.rejected = {}
        for symbol, quote in quotes.items():
            problem = quote_problem(quote)
            if problem:
                self.rejected[symbol] = problem
                continue
            seen = (quote["bid"], quote["ask"], float(quote["time"]))
            previous = self._last_seen.get(symbol)
            self._last_seen[symbol] = seen
            if previous is None or previous == seen:
                continue  # the first sighting and a repeat both prove nothing
            offset = estimate_server_offset(float(quote["time"]), received_at.timestamp())
            if offset is None:
                self.rejected[symbol] = "implausible timestamp"
                continue
            if self.server_offset is not None:
                ahead = float(quote["time"]) - self.server_offset - received_at.timestamp()
                if ahead > MAX_FUTURE_SECONDS:
                    self.rejected[symbol] = "timestamp in the future"
                    continue
            offsets.append(offset)
            self.last_change_by_symbol[symbol] = received_at
            self.last_change = received_at
        if offsets:
            self.server_offset = int(statistics.mode(offsets))

    def ticking(self, now: datetime, window_seconds: float) -> list[str]:
        horizon = now - timedelta(seconds=window_seconds)
        return sorted(s for s, at in self.last_change_by_symbol.items() if at >= horizon)


def completed_bars(
    rates,
    *,
    timeframe: str,
    server_offset: int,
    point: float,
    now: datetime,
) -> list[Candle]:
    """Broker rates as closed, UTC-stamped, mid-priced bars. A rate that is
    still forming, or not internally consistent, is dropped."""
    minutes = universe.TIMEFRAMES[timeframe]
    span = timedelta(minutes=minutes)
    bars: list[Candle] = []
    for rate in rates or ():
        ts = datetime.fromtimestamp(int(rate["time"]) - server_offset, tz=UTC)
        if ts + span > now:
            continue
        half = float(rate["spread"]) * point / 2
        open_, high, low, close = (float(rate[k]) + half for k in ("open", "high", "low", "close"))
        if high < max(open_, close) or low > min(open_, close) or low <= 0:
            continue
        bars.append(Candle(ts, open_, high, low, close, float(rate["spread"]) * point,
                           float(rate["tick_volume"])))
    return bars


def aggregate(hourly: list[Candle], timeframe: str) -> list[Candle]:
    """H4 or D1 from closed H1 bars, on the UTC boundaries the recorded store
    uses. A bucket whose last hour has not closed is dropped, so nothing here
    can present a part-formed bar as complete."""
    minutes = universe.TIMEFRAMES[timeframe]
    span = timedelta(minutes=minutes)
    epoch = datetime(2000, 1, 3, tzinfo=UTC)
    buckets: dict[datetime, list[Candle]] = {}
    for bar in sorted(hourly, key=lambda b: b.ts):
        offset = (bar.ts - epoch) % span
        buckets.setdefault(bar.ts - offset, []).append(bar)
    if not hourly:
        return []
    newest_close = max(b.ts for b in hourly) + timedelta(hours=1)
    return [
        Candle(
            start, bars[0].open, max(b.high for b in bars), min(b.low for b in bars),
            bars[-1].close, sum(b.spread for b in bars) / len(bars), sum(b.volume for b in bars),
        )
        for start, bars in sorted(buckets.items())
        if start + span <= newest_close
    ]


@dataclass
class FeedState:
    connection: FeedConnection
    detail: str
    provider: str | None
    instruments_expected: int
    instruments_ticking: int
    instruments_missing: list[str]
    server_offset_seconds: int | None

    def as_dict(self) -> dict:
        return {
            "connection": self.connection.value,
            "detail": self.detail,
            "provider": self.provider,
            "instruments_expected": self.instruments_expected,
            "instruments_ticking": self.instruments_ticking,
            "instruments_missing": self.instruments_missing,
            "server_offset_seconds": self.server_offset_seconds,
        }


class FeedBridge:
    """Keeps the service's feed inputs current. `step` does one pass and takes
    no locks of its own, so tests drive it directly with a fake terminal."""

    def __init__(
        self,
        service_getter: Callable[[], Any],
        provider,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        symbol_suffix: str | None = None,
    ):
        self._service = service_getter
        self._provider = provider
        self._clock = clock
        self._suffix = os.environ.get("TARS_MT5_SYMBOL_SUFFIX", "") if symbol_suffix is None else symbol_suffix
        self.tracker = TickTracker()
        self._last_bars_at: datetime | None = None
        self._hourly: dict[str, dict[datetime, Candle]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._symbols = {s + self._suffix: s for s in universe.SYMBOLS}
        provider.watch(list(self._symbols))

    def _identity(self) -> str:
        server = self._provider.server_name()
        return "mt5:" + server if server else "mt5"

    def step(self) -> FeedState:
        now = self._clock()
        snapshot = self._provider.snapshot()
        connection = connection_from_provider(snapshot["state"])
        service = self._service()

        if connection is not FeedConnection.CONNECTED:
            state = self._state(connection, snapshot["detail"], {}, now)
            if service is not None:
                service.report_feed(state, last_tick=None, connected=False)
            return state

        quotes = {self._symbols[k]: v for k, v in snapshot["quotes"].items() if k in self._symbols}
        self.tracker.observe(quotes, now)
        state = self._state(connection, "connected", quotes, now)
        if service is not None:
            service.report_feed(state, last_tick=self.tracker.last_change, connected=True)
            if self._bars_due(now):
                self._refresh_bars(service, now)
        return state

    def _state(self, connection: FeedConnection, detail: str, quotes: dict, now: datetime) -> FeedState:
        return FeedState(
            connection=connection,
            detail=detail,
            provider=self._identity() if connection is FeedConnection.CONNECTED else None,
            instruments_expected=len(self._symbols),
            instruments_ticking=len(self.tracker.ticking(now, 90.0)),
            instruments_missing=sorted(set(self._symbols.values()) - set(quotes)),
            server_offset_seconds=self.tracker.server_offset,
        )

    def _bars_due(self, now: datetime) -> bool:
        # Without the broker's offset a bar cannot be placed in UTC at all.
        if self.tracker.server_offset is None:
            return False
        if self._last_bars_at is None:
            return True
        return (now - self._last_bars_at).total_seconds() >= BARS_REFRESH_SECONDS

    def _rate_count(self, timeframe: str, now: datetime) -> int:
        """Everything on the first pass, then only what has closed since the
        last one. That also covers a reconnect: the gap sets the count."""
        full = RATE_COUNTS[timeframe]
        if self._last_bars_at is None:
            return full
        elapsed = (now - self._last_bars_at).total_seconds() / 60
        missing = int(elapsed / universe.TIMEFRAMES[timeframe]) + 3
        return min(full, max(missing, _MIN_HOURLY_BARS if timeframe == "H1" else 0))

    def _refresh_bars(self, service, now: datetime) -> None:
        counts = {tf: self._rate_count(tf, now) for tf in RATE_COUNTS}
        self._last_bars_at = now
        offset = self.tracker.server_offset
        edges: list[datetime] = []
        for broker_symbol, symbol in self._symbols.items():
            point = self._provider.point(broker_symbol)
            if not point or offset is None:
                continue
            for timeframe, count in counts.items():
                rates = self._provider.read_rates(broker_symbol, universe.TIMEFRAMES[timeframe], count)
                bars = completed_bars(
                    rates, timeframe=timeframe, server_offset=offset, point=point, now=now
                )
                service.store.put_live_bars(symbol, timeframe, bars)
                if timeframe == "H1":
                    self._hourly.setdefault(symbol, {}).update({b.ts: b for b in bars})
                if timeframe == "M5" and bars:
                    edges.append(bars[-1].ts + timedelta(minutes=5))
            hourly = list(self._hourly.get(symbol, {}).values())
            service.store.put_live_bars(symbol, "H4", aggregate(hourly, "H4"))
            service.store.put_live_bars(symbol, "D1", aggregate(hourly, "D1"))
        if edges:
            # The slowest instrument sets the edge, the same rule the recorded
            # bounds use: a panel must not outrun any of its inputs.
            service.report_live_edge(min(edges))

    # ---- background loop -------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="market-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001 - a feed fault must never reach the app
                logger.warning("market feed step failed: %s", type(exc).__name__, exc_info=True)
