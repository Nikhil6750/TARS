"""DailyMarketBriefService -- once-per-local-day brief (mission: TARS
Watcher Intelligence, sections 14-16/23 J/K/L). Uses the real AppStateStore
migration (same fixture pattern as test_app_state.py/test_watchlist.py);
monitors/synthesis provider are fakes so content/dedupe logic is isolated
from live network/LLM calls.
"""
from __future__ import annotations

from datetime import datetime

import aiosqlite
import pytest

from assistant.provider import AssistantProvider, AssistantReply
from storage.app_state import AppStateStore
from storage.migrator import run_migrations
from trading.daily_brief import DailyMarketBriefService
from trading.watchlist import WatchlistStore


class _FakeMonitors:
    def __init__(self, *, mt5_state="CONNECTED", calendar_state="CONNECTED", with_news=True):
        self._mt5_state = mt5_state
        self._calendar_state = calendar_state
        self.news = _FakeNews() if with_news else None

    async def status(self) -> dict:
        return {
            "mt5": {"state": self._mt5_state, "positions": []},
            "calendar": {"state": self._calendar_state,
                        "next": {"currency": "USD", "event": "CPI", "at": "2026-01-01T13:30:00Z",
                                "importance": "High"} if self._calendar_state == "CONNECTED" else None},
        }


class _FakeNews:
    def latest(self, symbols=None, limit=10):
        return [{"headline": f"Headline about {symbols[0] if symbols else 'market'}"}]


class _FakeSynthesisProvider(AssistantProvider):
    name = "fake_synthesis"

    def __init__(self):
        self.calls = []

    async def respond(self, request):
        self.calls.append(request)
        return AssistantReply(text="Markets were quiet overnight.", provider=self.name)


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "daily_brief_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


def _service(conn, *, monitors=None, synthesis=None, today="2026-03-05"):
    app_state = AppStateStore(conn)
    watchlist = WatchlistStore(app_state)
    monitors = monitors or _FakeMonitors()
    synthesis = synthesis or _FakeSynthesisProvider()
    clock = lambda: datetime.fromisoformat(f"{today}T09:00:00")
    service = DailyMarketBriefService(
        app_state, watchlist, monitors, synthesis,
        readiness_timeout_seconds=0.0, readiness_poll_seconds=0.0, clock=clock,
    )
    return service, app_state, watchlist, synthesis


# ---- J: once-per-day -------------------------------------------------------

async def test_fresh_local_date_generates_one_automatic_brief(conn):
    service, app_state, _watchlist, synthesis = _service(conn)
    result = await service.maybe_generate_on_startup()
    assert result is not None
    assert result.status == "GENERATED"
    assert result.date == "2026-03-05"
    assert len(synthesis.calls) == 1
    assert await app_state.get("last_daily_brief_date") == "2026-03-05"


async def test_same_day_restart_does_not_auto_replay(conn):
    service, app_state, _watchlist, synthesis = _service(conn)
    await service.maybe_generate_on_startup()

    # "restart": a brand new service instance against the same durable state.
    second, _app_state2, _watchlist2, synthesis2 = _service(conn, synthesis=synthesis)
    result = await second.maybe_generate_on_startup()
    assert result is None
    assert len(synthesis.calls) == 1  # still just the one real generation


# ---- K: next-day ------------------------------------------------------------

async def test_next_local_date_is_eligible_for_a_new_automatic_brief(conn):
    service, _app_state, _watchlist, synthesis = _service(conn, today="2026-03-05")
    await service.maybe_generate_on_startup()

    tomorrow, _, _, synthesis2 = _service(conn, synthesis=synthesis, today="2026-03-06")
    result = await tomorrow.maybe_generate_on_startup()
    assert result is not None
    assert result.date == "2026-03-06"
    assert len(synthesis.calls) == 2


# ---- explicit replay --------------------------------------------------------

async def test_explicit_replay_always_regenerates_regardless_of_dedupe(conn):
    service, _app_state, _watchlist, synthesis = _service(conn)
    await service.maybe_generate_on_startup()
    assert len(synthesis.calls) == 1

    result = await service.generate(force=True)
    assert result.status == "GENERATED"
    assert len(synthesis.calls) == 2


# ---- L: provider degradation -------------------------------------------------

async def test_mt5_disconnected_still_produces_a_brief_truthfully_degraded(conn):
    monitors = _FakeMonitors(mt5_state="DISCONNECTED_AUTH_REQUIRED")
    service, _app_state, _watchlist, synthesis = _service(conn, monitors=monitors)
    result = await service.maybe_generate_on_startup()
    assert result.status == "GENERATED"
    assert "mt5" in result.degraded_sources
    request = synthesis.calls[0]
    assert "DISCONNECTED_AUTH_REQUIRED" in request.system_context
    assert "MT5" in request.system_context


async def test_watched_assets_are_named_in_the_evidence(conn):
    service, _app_state, watchlist, synthesis = _service(conn)
    await watchlist.add("gold", "XAUUSD")
    await service.maybe_generate_on_startup()
    assert "XAUUSD" in synthesis.calls[0].system_context


async def test_no_watched_assets_is_reported_honestly_not_fabricated(conn):
    service, _app_state, _watchlist, synthesis = _service(conn)
    await service.maybe_generate_on_startup()
    assert "No assets are currently on the watchlist" in synthesis.calls[0].system_context
