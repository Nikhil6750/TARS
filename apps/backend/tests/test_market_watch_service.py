"""MarketWatchService -- the local TARS watcher (mission: TARS Watcher
Intelligence). Uses the REAL RealtimeEventCore/SignificanceGate (in-memory
sqlite, same pattern as test_realtime_events.py) so correlation/dedupe/
escalation are exercised against the actual admission pipeline, not a mock
of it -- only the orchestrator (the Claude call itself) and the monitor
providers are fakes.
"""
from __future__ import annotations

import aiosqlite
import pytest

from agents.models import AgentRunResult, AgentRunStatus
from events.core import RealtimeEventCore, SignificanceGate
from storage.app_state import AppStateStore
from storage.migrator import run_migrations
from trading.asset_resolver import AssetResolver
from trading.market_watch_service import (
    EventCandidate,
    MarketWatchService,
    requires_reasoning,
)
from trading.watchlist import WatchlistStore


# ---- requires_reasoning() -- pure function, no fixtures needed ------------

def test_single_calendar_event_alone_does_not_require_reasoning():
    candidate = EventCandidate(symbol="EURUSD", has_calendar_event=True, calendar_title="CPI in 15 min")
    assert requires_reasoning(candidate) is False


def test_two_correlated_factors_require_reasoning():
    candidate = EventCandidate(symbol="EURUSD", has_calendar_event=True, has_price_move=True, move_pct=0.4)
    assert requires_reasoning(candidate) is True


def test_unusual_move_without_other_cause_requires_reasoning():
    candidate = EventCandidate(symbol="EURUSD", has_price_move=True, move_is_unusual_without_obvious_cause=True)
    assert requires_reasoning(candidate) is True


def test_ordinary_move_without_other_cause_does_not_require_reasoning():
    candidate = EventCandidate(symbol="EURUSD", has_price_move=True, move_is_unusual_without_obvious_cause=False)
    assert requires_reasoning(candidate) is False


def test_open_position_plus_calendar_requires_reasoning():
    candidate = EventCandidate(symbol="EURUSD", has_calendar_event=True, has_open_position=True)
    assert requires_reasoning(candidate) is True


def test_provider_disconnected_notice_does_not_require_reasoning():
    candidate = EventCandidate(symbol="EURUSD")  # no factors at all
    assert requires_reasoning(candidate) is False


# ---- integration-style fixtures -------------------------------------------

class _FakeMonitors:
    def __init__(self):
        self.mt5 = _FakeMT5()
        self.calendar = _FakeSymbolList()
        self.news = _FakeSymbolList()


class _FakeSymbolList:
    def __init__(self):
        self.symbols: list[str] = []


class _FakeMT5:
    def __init__(self):
        self.symbols: list[str] = []
        self._quotes: dict[str, dict] = {}
        self._positions: list[dict] = []

    def set_quote(self, symbol: str, bid: float, ask: float) -> None:
        self._quotes[symbol] = {"bid": bid, "ask": ask}

    def snapshot(self) -> dict:
        return {"quotes": dict(self._quotes), "positions": list(self._positions)}


class _Runtime:
    def __init__(self):
        self.calls = []

    async def run_on_demand(self, agent, **kwargs):
        self.calls.append(agent)
        return AgentRunResult(status=AgentRunStatus.SUCCEEDED, summary="Grounded read.")


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "market_watch_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


@pytest.fixture
async def rig(conn):
    """One fully-wired (but fake-provider) watcher: real RealtimeEventCore
    + real SignificanceGate + real WatchlistStore/AppStateStore, fake
    monitors/orchestrator."""
    runtime = _Runtime()
    # Baseline deliberately does NOT include EURUSD/XAUUSD/BTCUSD -- these
    # tests watch those symbols fresh, so they exercise the NEW watchlist-
    # only NOTIFY-cap mechanism (SignificanceGate.allow_symbol(analyze=
    # False)) rather than colliding with a pre-existing fully-analyze-
    # eligible baseline (which is what settings.event_relevant_symbols
    # would already grant EURUSD/XAUUSD in real production -- a separate,
    # pre-existing concern this mission does not change).
    gate = SignificanceGate(symbols=["GBPUSD"], analyze=True, speak=False)
    core = RealtimeEventCore(conn, runtime, orchestrator=None, gate=gate, cooldown=0)
    await core.start()
    monitors = _FakeMonitors()
    app_state = AppStateStore(conn)
    watchlist = WatchlistStore(app_state)
    service = MarketWatchService(
        watchlist, AssetResolver(monitors), monitors, core, gate,
        app_state_store=app_state, cooldown_seconds=1800.0, correlation_window_seconds=1800.0,
    )
    yield service, core, monitors, runtime, watchlist
    await core.close()


async def _publish_price_move(core, symbol: str, move_pct: float, dedupe_suffix: str = "a"):
    from events.core import EventSource, NormalizedEvent

    await core.publish(NormalizedEvent(
        source=EventSource.MT5, kind="price.move", severity=3, symbol=symbol,
        title=f"{symbol} moved", summary="", dedupe_key=f"{symbol}:move:{dedupe_suffix}",
        payload={"move_pct": move_pct, "from": 1.1, "to": 1.1 + move_pct / 100},
    ))


async def _publish_calendar(core, symbol: str, title: str, dedupe_suffix: str = "cal"):
    from events.core import EventSource, NormalizedEvent

    await core.publish(NormalizedEvent(
        source=EventSource.ECONOMIC_CALENDAR, kind="calendar.upcoming", severity=3, symbol=symbol,
        title=title, summary="", dedupe_key=f"{symbol}:cal:{dedupe_suffix}",
        payload={"event": title},
    ))


async def _publish_news(core, symbol: str, headline: str, dedupe_suffix: str = "news"):
    from events.core import EventSource, NormalizedEvent

    await core.publish(NormalizedEvent(
        source=EventSource.NEWS, kind="news.headline", severity=2, symbol=symbol,
        title=headline, summary="", dedupe_key=f"{symbol}:news:{dedupe_suffix}",
        payload={},
    ))


# ---- A: watchlist persistence ---------------------------------------------

async def test_watch_resolves_and_persists_then_survives_a_restart(rig):
    service, core, monitors, runtime, watchlist = rig
    await service.start()
    resolved, entry = await service.watch("EURUSD")
    assert resolved.outcome == "RESOLVED"
    assert entry.canonical_symbol == "EURUSD"
    await service.stop()

    # "restart": a brand new service instance against the same durable state.
    fresh = MarketWatchService(watchlist, AssetResolver(monitors), monitors, core, core.gate,
                               app_state_store=AppStateStore(core.conn))
    await fresh.start()
    watched = await fresh.list_watched()
    assert [e.canonical_symbol for e in watched] == ["EURUSD"]
    await fresh.stop()


# ---- B: follow-up "watch this" ---------------------------------------------

async def test_watch_this_uses_active_symbol_follow_up_context(rig):
    service, core, monitors, runtime, watchlist = rig
    await service.start()
    resolved, entry = await service.watch("this", active_symbol="XAUUSD")
    assert resolved.outcome == "RESOLVED"
    assert entry.canonical_symbol == "XAUUSD"
    await service.stop()


async def test_watch_this_without_any_active_asset_is_truthfully_not_found(rig):
    service, core, monitors, runtime, watchlist = rig
    await service.start()
    result = await service.watch("this")
    assert result.outcome == "NOT_FOUND"
    assert await service.list_watched() == []
    await service.stop()


# ---- C: ambiguity -----------------------------------------------------------

async def test_ambiguous_asset_never_creates_a_watch_entry(rig):
    service, core, monitors, runtime, watchlist = rig
    await service.start()
    result = await service.watch("nasdaq")
    assert result.outcome == "AMBIGUOUS"
    assert await service.list_watched() == []
    await service.stop()


# ---- D: local filtering -- insignificant changes never reach Claude -------

async def test_insignificant_price_wobbles_produce_no_alert_and_no_claude_call(rig):
    service, core, monitors, runtime, watchlist = rig
    await service.start()
    await service.watch("bitcoin")
    monitors.mt5.set_quote("BTCUSD", 50000.0, 50001.0)
    for _ in range(10):
        await service.tick()
        monitors.mt5.set_quote("BTCUSD", 50001.0, 50002.0)  # trivial wobble each cycle
    assert service.alerts_published == 0
    assert runtime.calls == []
    await service.stop()


# ---- E: correlated material event -> one alert, at most one Claude call ---

async def test_correlated_calendar_plus_price_yields_one_alert_one_claude_call(rig):
    """Mission acceptance test E's literal scenario: a high-impact release
    plus a material move on the watched symbol."""
    service, core, monitors, runtime, watchlist = rig
    await service.start()
    await service.watch("EURUSD")

    await _publish_calendar(core, "EURUSD", "US CPI release")
    await _publish_price_move(core, "EURUSD", 0.6)
    await core._queue.join()

    assert service.alerts_published == 1
    assert len(runtime.calls) == 1  # requires_reasoning() -> ANALYZE -> exactly one Claude call
    await service.stop()


# ---- F: dedupe -- same evidence across many cycles still one alert --------

async def test_dedupe_same_correlated_story_across_several_cycles_is_one_alert(rig):
    service, core, monitors, runtime, watchlist = rig
    await service.start()
    await service.watch("EURUSD")

    await _publish_calendar(core, "EURUSD", "US CPI release")
    await _publish_price_move(core, "EURUSD", 0.6)
    await core._queue.join()
    assert service.alerts_published == 1

    # The SAME two events are re-delivered (simulating repeated polling
    # cycles observing the same still-current evidence) -- must not spam.
    for _ in range(5):
        await service._on_event({"type": "proactive_event",
                                 "event": {"source": "ECONOMIC_CALENDAR", "kind": "calendar.upcoming",
                                          "symbol": "EURUSD", "title": "US CPI release",
                                          "dedupe_key": "EURUSD:cal:cal",
                                          "payload": {"event": "US CPI release"}}})
    assert service.alerts_published == 1
    await service.stop()


# ---- G: new development overrides dedupe -----------------------------------

async def test_a_materially_new_development_is_allowed_through(rig):
    service, core, monitors, runtime, watchlist = rig
    await service.start()
    await service.watch("EURUSD")

    await _publish_calendar(core, "EURUSD", "US CPI release")
    await _publish_price_move(core, "EURUSD", 0.6, dedupe_suffix="first")
    await core._queue.join()
    assert service.alerts_published == 1

    # A genuinely new headline joins the story -- different evidence
    # signature, so this must be allowed through even within cooldown.
    await _publish_news(core, "EURUSD", "Fed official hints at emergency meeting")
    await core._queue.join()
    assert service.alerts_published == 2
    await service.stop()


# ---- H: upcoming high-impact event alone -> deterministic, no Claude ------

async def test_approaching_high_impact_event_alone_is_deterministic_not_claude(rig):
    service, core, monitors, runtime, watchlist = rig
    await service.start()
    await service.watch("bitcoin")  # BTCUSD -- newly watched, not analyze-eligible on its own

    await _publish_calendar(core, "BTCUSD", "Major crypto regulation vote")
    await core._queue.join()

    # The raw calendar event itself is still visible (NOTIFY), but no
    # correlated SYSTEM alert/Claude call for a single factor alone.
    assert service.alerts_published == 0
    assert runtime.calls == []
    assert any(r["decision"] == "NOTIFY" for r in core.recent)
    await service.stop()


# ---- I: Claude gating across many ordinary cycles --------------------------

async def test_claude_calls_are_dramatically_lower_than_cycles_and_events(rig):
    service, core, monitors, runtime, watchlist = rig
    await service.start()
    await service.watch("EURUSD")
    await service.watch("bitcoin")

    monitors.mt5.set_quote("EURUSD", 1.1000, 1.1001)
    monitors.mt5.set_quote("BTCUSD", 50000.0, 50001.0)
    for i in range(50):
        await service.tick()  # 50 ordinary watcher cycles, trivial price drift
        monitors.mt5.set_quote("EURUSD", 1.1000 + i * 0.000001, 1.1001 + i * 0.000001)

    # One genuinely correlated story in the middle of all that noise.
    await _publish_calendar(core, "EURUSD", "ECB rate decision")
    await _publish_price_move(core, "EURUSD", 0.7)
    await core._queue.join()

    assert service.cycles == 50
    assert len(runtime.calls) <= 1
    assert len(runtime.calls) < service.cycles
    await service.stop()


# ---- M: pause/resume --------------------------------------------------------

async def test_pause_suppresses_correlated_alerts_without_dropping_the_watchlist(rig):
    service, core, monitors, runtime, watchlist = rig
    await service.start()
    await service.watch("EURUSD")
    await service.pause()
    assert service.is_paused() is True

    await _publish_calendar(core, "EURUSD", "US CPI release")
    await _publish_price_move(core, "EURUSD", 0.6)
    await core._queue.join()
    assert service.alerts_published == 0

    watched = await service.list_watched()
    assert [e.canonical_symbol for e in watched] == ["EURUSD"]

    await service.resume()
    assert service.is_paused() is False
    # Paused evidence was never recorded (full stop, not just "don't
    # alert") -- fresh evidence after resume must still correlate normally.
    await _publish_calendar(core, "EURUSD", "US CPI release", dedupe_suffix="post-resume")
    await _publish_price_move(core, "EURUSD", 0.6, dedupe_suffix="post-resume")
    await core._queue.join()
    assert service.alerts_published == 1
    await service.stop()


async def test_pause_state_persists_across_a_restart(rig):
    service, core, monitors, runtime, watchlist = rig
    await service.start()
    await service.pause()
    await service.stop()

    fresh = MarketWatchService(watchlist, AssetResolver(monitors), monitors, core, core.gate,
                               app_state_store=AppStateStore(core.conn))
    await fresh.start()
    assert fresh.is_paused() is True
    await fresh.stop()


# ---- unwatch -----------------------------------------------------------------

async def test_unwatch_removes_from_watchlist_and_stops_admitting_its_events(rig):
    service, core, monitors, runtime, watchlist = rig
    await service.start()
    await service.watch("bitcoin")
    assert "BTCUSD" in monitors.mt5.symbols

    resolved, removed = await service.unwatch("bitcoin")
    assert removed is True
    assert "BTCUSD" not in monitors.mt5.symbols
    assert await service.list_watched() == []
    await service.stop()


# ---- sleep/resume rebase (section 11/21 I) ----------------------------------

async def test_rebase_clears_price_history_and_evidence_without_touching_watchlist(rig):
    service, core, monitors, runtime, watchlist = rig
    await service.start()
    await service.watch("EURUSD")
    monitors.mt5.set_quote("EURUSD", 1.1000, 1.1001)
    await service.tick()
    assert len(service._price_history["EURUSD"]) == 1

    await _publish_calendar(core, "EURUSD", "US CPI release")
    await core._queue.join()
    assert len(service._evidence["EURUSD"]) == 1

    service.rebase()

    assert len(service._price_history["EURUSD"]) == 0
    assert len(service._evidence["EURUSD"]) == 0
    watched = await service.list_watched()
    assert [e.canonical_symbol for e in watched] == ["EURUSD"]  # untouched
    status = await service.status()
    assert status["last_rebase_at"] is not None
    await service.stop()


async def test_rebase_clears_dedupe_memory_so_a_post_resume_alert_is_not_suppressed(rig):
    service, core, monitors, runtime, watchlist = rig
    await service.start()
    await service.watch("EURUSD")

    await _publish_calendar(core, "EURUSD", "US CPI release")
    await _publish_price_move(core, "EURUSD", 0.6)
    await core._queue.join()
    assert service.alerts_published == 1

    service.rebase()

    # The identical story would normally be suppressed by cooldown -- but
    # rebase cleared the dedupe memory, so a fresh correlated story (even
    # with the same evidence shape) after a rebase is allowed through.
    await _publish_calendar(core, "EURUSD", "US CPI release", dedupe_suffix="cal")
    await _publish_price_move(core, "EURUSD", 0.6, dedupe_suffix="a")
    await core._queue.join()
    assert service.alerts_published == 2
    await service.stop()


# ---- provider degradation (section 19 / L) ----------------------------------

async def test_mt5_disconnected_snapshot_does_not_crash_the_tick(rig):
    service, core, monitors, runtime, watchlist = rig
    await service.start()
    await service.watch("EURUSD")

    async def _raise():
        raise RuntimeError("disconnected")

    monitors.mt5.snapshot = lambda: (_ for _ in ()).throw(RuntimeError("disconnected"))
    await service.tick()  # must not raise
    assert service.cycles == 1
    await service.stop()
