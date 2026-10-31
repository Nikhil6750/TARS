"""The live feed, end to end, against a fake terminal.

This drives the real `MT5Provider`, the real `FeedBridge` and the real service
and store. Only the `MetaTrader5` module is faked, so a pass here proves the
state machine and says nothing about a real broker. That verification is
blocked on an authenticated account and an open market.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import duckdb
import pytest

from market import clock, feed, universe
from market import state as market_state
from market.clock import FeedStatus
from market.provenance import DataSource
from market.service import MarketXRayService
from market.store import Candle, CandleStore
from monitors.mt5_provider import MT5Provider, MT5State

# Wednesday mid-session, so nothing is CLOSED by accident of the calendar.
NOW = datetime(2026, 6, 3, 12, 2, 30, tzinfo=UTC)
SERVER_OFFSET = 3 * 3600  # a UTC+3 broker
RECORDED_END = datetime(2026, 5, 29, 21, 0, tzinfo=UTC)


class FakeTerminal:
    """The surface of the MetaTrader5 module the provider uses, read-only."""

    TIMEFRAME_M5, TIMEFRAME_M15, TIMEFRAME_H1 = 5, 15, 16385

    def __init__(self, now):
        self.now = now
        self.connected = True
        self.authorised = True
        self.price = 1.1
        self.frozen = False
        self.tick_epoch = None
        self.calls = []

    def initialize(self):
        return self.authorised

    def last_error(self):
        return (-6, "Terminal: Authorization failed")

    def terminal_info(self):
        return SimpleNamespace(connected=self.connected) if self.authorised else None

    def account_info(self):
        return SimpleNamespace(login=12345678, server="Fake-Demo")

    def symbol_select(self, symbol, enable):
        return True

    def symbol_info(self, symbol):
        return SimpleNamespace(point=0.00001)

    def symbol_info_tick(self, symbol):
        if not self.frozen:
            self.tick_epoch = self.now().timestamp() + SERVER_OFFSET
        return SimpleNamespace(bid=self.price, ask=self.price + 0.0001, time=int(self.tick_epoch))

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        minutes = {5: 5, 15: 15, 16385: 60}[timeframe]
        span = minutes * 60
        newest = int(self.now().timestamp() + SERVER_OFFSET) // span * span
        return [
            {"time": newest - span * i, "open": 1.1, "high": 1.102, "low": 1.098,
             "close": 1.101, "spread": 10, "tick_volume": 5}
            for i in reversed(range(min(count, 400)))
        ]

    def positions_get(self):
        return []

    def orders_get(self):
        return []

    def shutdown(self):
        pass


class Rig:
    """A service over a small recorded store, wired to a fake terminal."""

    def __init__(self, tmp_path, store_root):
        self.now = NOW
        self.terminal = FakeTerminal(lambda: self.now)
        self.store = CandleStore(store_root)
        self.service = MarketXRayService(
            store=self.store, state=market_state.MarketState(tmp_path / "state.db")
        )

        async def publish(event):
            return True

        async def on_state(state):
            return None

        self.provider = MT5Provider(
            [], publish, on_state, module_loader=lambda: self.terminal, process_checker=lambda: True
        )
        self.bridge = feed.FeedBridge(
            lambda: self.service, self.provider, clock=lambda: self.now, symbol_suffix=""
        )

    def poll(self, advance: float = 2.0, *, move: bool = True) -> None:
        self.now += timedelta(seconds=advance)
        if move:
            self.terminal.price += 0.0001
        snapshot = self.provider.poll_once()
        asyncio.run(self.provider._apply(snapshot))
        self.bridge.step()

    def status(self):
        with _market_open_at(self):
            return self.service.market_status()

    def provenance(self):
        with _market_open_at(self):
            return self.service.data_provenance()


class _market_open_at:
    """Judges the status at the rig's simulated moment, not the wall clock."""

    def __init__(self, rig):
        self.rig = rig

    def __enter__(self):
        self.original = clock.datetime

        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return self.rig.now

        import market.service as module
        self.module = module
        self.real = module.datetime
        module.datetime = Frozen

    def __exit__(self, *exc):
        self.module.datetime = self.real


def _write_recorded_store(root) -> None:
    for symbol in universe.SYMBOLS:
        directory = root / symbol
        directory.mkdir(parents=True)
        for timeframe, minutes in universe.TIMEFRAMES.items():
            start = RECORDED_END - timedelta(minutes=minutes * 300)
            rows = ", ".join(
                f"(TIMESTAMP '{(start + timedelta(minutes=minutes * i)).strftime('%Y-%m-%d %H:%M:%S')}',"
                f" 1.0, 1.01, 0.99, {1.0 + (i % 5) * 0.001}, 0.0001, 1.0)"
                for i in range(300)
            )
            target = str(directory / f"{timeframe}.parquet").replace("'", "''")
            duckdb.sql(
                f"COPY (SELECT * FROM (VALUES {rows}) t(ts, open, high, low, close, spread, volume)) "
                f"TO '{target}' (FORMAT PARQUET)"
            )


@pytest.fixture(scope="module")
def store_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("recorded")
    _write_recorded_store(root)
    return root


@pytest.fixture
def rig(tmp_path, store_root):
    return Rig(tmp_path, store_root)


def live(rig) -> None:
    """Enough polls for a changing tick and the first bars."""
    rig.poll()
    rig.poll()
    rig.poll()


# ---- what must be true before anything says LIVE ------------------------


def test_a_connected_feed_with_no_bars_yet_is_not_live(rig):
    """The hole in the first version of this gate: a fresh tick made the
    status LIVE while every panel was still recorded history."""
    rig.service.attach_feed(provider="mt5:x", last_tick=rig.now)
    status = rig.status()
    assert status.status is FeedStatus.REPLAY
    prov = rig.provenance()
    assert prov.is_live is False
    assert prov.source is DataSource.RECORDED_STORE
    assert "not delivered bars" in prov.detail


def test_the_first_quote_after_a_start_proves_nothing(rig):
    rig.poll()
    assert rig.bridge.tracker.last_change is None
    assert rig.status().status is FeedStatus.REPLAY


def test_changing_ticks_and_bars_produce_live(rig):
    live(rig)
    status = rig.status()
    assert status.status is FeedStatus.LIVE
    assert status.source == DataSource.LIVE_FEED.value
    assert status.feed_connection == "CONNECTED"
    prov = rig.provenance()
    assert prov.is_live is True
    assert prov.provider == "mt5:Fake-Demo"


def test_live_panels_are_computed_from_the_live_bars(rig):
    live(rig)
    cursor = rig.service.cursor
    assert cursor > RECORDED_END
    bars = rig.service.store.candles("EURUSD", "M5", as_of=cursor, limit=5)
    assert all(b.ts > RECORDED_END for b in bars)
    assert bars[-1].close == pytest.approx(1.101 + 10 * 0.00001 / 2)


# ---- broker time ---------------------------------------------------------


def test_the_broker_offset_is_measured_not_assumed(rig):
    live(rig)
    assert rig.bridge.tracker.server_offset == SERVER_OFFSET


def test_bars_are_placed_in_utc(rig):
    live(rig)
    newest = rig.service.store.candles("EURUSD", "M5", as_of=rig.service.cursor, limit=1)[-1]
    assert newest.ts + timedelta(minutes=5) <= rig.now
    assert rig.now - newest.ts < timedelta(minutes=10), "bars are hours out: offset not removed"


def test_the_bar_still_forming_is_dropped():
    now = datetime(2026, 6, 3, 12, 2, tzinfo=UTC)
    forming = {"time": int(datetime(2026, 6, 3, 12, 0, tzinfo=UTC).timestamp()), "open": 1, "high": 1,
               "low": 1, "close": 1, "spread": 0, "tick_volume": 1}
    closed = {**forming, "time": forming["time"] - 300}
    bars = feed.completed_bars([closed, forming], timeframe="M5", server_offset=0, point=0.00001, now=now)
    assert [b.ts.minute for b in bars] == [55]


def test_implausible_timestamps_are_rejected():
    tracker = feed.TickTracker()
    tracker.observe({"EURUSD": {"bid": 1.0, "ask": 1.1, "time": 1000}}, NOW)
    tracker.observe({"EURUSD": {"bid": 1.1, "ask": 1.2, "time": 2000}}, NOW)
    assert tracker.last_change is None
    assert tracker.rejected["EURUSD"] == "implausible timestamp"


@pytest.mark.parametrize(
    "quote,reason",
    [
        ({"bid": 0, "ask": 1.0, "time": 1}, "non-positive price"),
        ({"bid": 1.1, "ask": 1.0, "time": 1}, "crossed quote"),
        ({"bid": 1.0, "ask": 1.1, "time": None}, "no timestamp"),
    ],
)
def test_impossible_quotes_are_rejected(quote, reason):
    assert feed.quote_problem(quote) == reason


# ---- staleness, disconnection and recovery ------------------------------


def test_a_frozen_terminal_goes_stale(rig):
    live(rig)
    rig.terminal.frozen = True
    rig.poll(advance=clock.LIVE_FRESHNESS_SECONDS + 5, move=False)
    status = rig.status()
    assert status.status is FeedStatus.STALE
    assert status.feed_connection == "CONNECTED"
    assert rig.provenance().is_live is False


def test_live_holds_inside_the_freshness_window(rig):
    live(rig)
    rig.terminal.frozen = True
    rig.poll(advance=clock.LIVE_FRESHNESS_SECONDS - 10, move=False)
    assert rig.status().status is FeedStatus.LIVE


def test_a_dropped_connection_is_stale_at_once_and_says_so(rig):
    live(rig)
    rig.terminal.connected = False
    rig.poll(move=False)
    status = rig.status()
    assert status.status is FeedStatus.STALE
    assert status.feed_connection == "DISCONNECTED"
    assert "disconnected" in status.detail.lower()
    prov = rig.provenance()
    assert prov.is_live is False
    assert prov.source is DataSource.LIVE_FEED, "the data shown did come from the feed"


def test_recovery_after_a_disconnect_needs_a_real_new_tick(rig):
    live(rig)
    rig.terminal.connected = False
    rig.poll(advance=300, move=False)
    assert rig.status().status is FeedStatus.STALE

    rig.terminal.connected = True
    rig.terminal.frozen = True
    rig.poll(move=False)
    assert rig.status().status is FeedStatus.STALE, "reconnecting alone proves nothing"

    rig.terminal.frozen = False
    rig.poll()
    rig.poll()
    assert rig.status().status is FeedStatus.LIVE


def test_recovery_fills_the_gap_in_the_bars(rig):
    live(rig)
    before = rig.service.cursor
    rig.terminal.connected = False
    rig.poll(advance=3600, move=False)
    rig.terminal.connected = True
    rig.poll(advance=30)
    rig.poll(advance=30)
    assert rig.service.cursor > before + timedelta(minutes=30)


def test_an_unauthenticated_terminal_is_named(rig):
    rig.terminal.authorised = False
    rig.poll(move=False)
    status = rig.status()
    assert status.feed_connection == "AUTH_REQUIRED"
    assert status.status is FeedStatus.REPLAY


def test_a_closed_market_is_never_live(rig):
    live(rig)
    rig.now = datetime(2026, 6, 6, 12, 0, tzinfo=UTC)  # a Saturday
    rig.terminal.frozen = True
    rig.poll(move=False)
    assert rig.status().status is FeedStatus.CLOSED
    assert rig.provenance().is_live is False


# ---- replay stays a replay ------------------------------------------------


def test_a_pinned_cursor_beats_a_perfect_feed(rig):
    live(rig)
    rig.service.set_cursor(RECORDED_END - timedelta(days=1))
    status = rig.status()
    assert status.status is FeedStatus.REPLAY
    prov = rig.provenance()
    assert prov.is_live is False
    assert prov.source is DataSource.RECORDED_STORE
    assert "pinned" in prov.detail


def test_replay_reads_recorded_bars_only(rig):
    live(rig)
    rig.service.set_cursor(RECORDED_END)
    assert rig.service.cursor == RECORDED_END
    bars = rig.service.store.candles("EURUSD", "M5", as_of=rig.service.cursor, limit=3)
    assert all(b.ts < RECORDED_END for b in bars)
    assert bars[-1].close != pytest.approx(1.101 + 10 * 0.00001 / 2)


def test_replay_cannot_be_moved_into_the_live_bars(rig):
    live(rig)
    assert rig.service.set_cursor(rig.now) == RECORDED_END


def test_leaving_replay_returns_to_live(rig):
    live(rig)
    rig.service.set_cursor(RECORDED_END - timedelta(days=1))
    rig.service.reset_cursor()
    assert rig.status().status is FeedStatus.LIVE
    assert rig.service.cursor > RECORDED_END


def test_the_gate_requires_every_condition():
    base = dict(
        now=NOW, feed_last_tick=NOW, cursor=RECORDED_END, store_last_bar=RECORDED_END,
        feed_connection="CONNECTED",
    )
    assert clock.resolve_status(**base, replay_pinned=False, panels_follow_feed=True).status is FeedStatus.LIVE
    assert clock.resolve_status(**base, replay_pinned=True, panels_follow_feed=True).status is FeedStatus.REPLAY
    assert clock.resolve_status(**base, replay_pinned=False, panels_follow_feed=False).status is FeedStatus.REPLAY


# ---- recorded data is untouched by a feed --------------------------------


def test_live_bars_never_replace_recorded_ones(rig):
    existing = rig.store.candles("EURUSD", "M5", as_of=RECORDED_END, limit=1)[-1]
    overlapping = Candle(existing.ts, 9.0, 9.0, 9.0, 9.0, 0.0, 1.0)
    rig.store.put_live_bars("EURUSD", "M5", [overlapping])
    assert rig.store.candles("EURUSD", "M5", as_of=RECORDED_END, limit=1)[-1].close == existing.close


def test_live_bars_do_not_extend_the_replay_bounds(rig):
    before = rig.store.store_bounds("M5")
    live(rig)
    assert rig.store.store_bounds("M5") == before


def test_h4_and_d1_are_cut_on_utc_boundaries_from_closed_hours():
    start = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    hours = [Candle(start + timedelta(hours=i), 1, 1.5, 0.5, 1 + i, 0, 1) for i in range(9)]
    h4 = feed.aggregate(hours, "H4")
    assert [b.ts.hour for b in h4] == [0, 4]
    assert h4[0].close == 4
    assert feed.aggregate(hours, "D1") == [], "a part-formed day must not appear as a bar"


# ---- read-only ------------------------------------------------------------


def test_the_provider_stays_read_only():
    import inspect

    import monitors.mt5_provider as module

    source = inspect.getsource(module) + inspect.getsource(feed)
    for call in ("order_send", "order_check", "order_calc", "positions_close"):
        assert call not in source


def test_rates_are_not_read_while_disconnected(rig):
    assert rig.provider.state is not MT5State.CONNECTED
    assert rig.provider.read_rates("EURUSD", 5, 10) is None


def test_no_credentials_are_used_or_stored(rig):
    import inspect

    import monitors.mt5_provider as module

    source = inspect.getsource(module) + inspect.getsource(feed)
    assert "login(" not in source
    assert "password" not in source.lower()
