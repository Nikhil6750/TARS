from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest

from agents.models import AgentRunResult, AgentRunStatus
from events.core import EventDecision, RealtimeEventCore, SignificanceGate
from monitors.calendar import (
    CalendarEvent,
    CalendarMonitor,
    EconomicCalendarProvider,
    FaireconomyCalendarProvider,
)
from monitors.manager import MonitorManager
from monitors.mt5_provider import MT5Provider, MT5State, mask_account


class Runtime:
    def __init__(self):
        self.calls = []

    async def run_on_demand(self, agent, **kwargs):
        self.calls.append(agent.event.title)
        return AgentRunResult(status=AgentRunStatus.SUCCEEDED, summary="analysis")


class FakeMT5:
    """Records every call so tests can prove nothing but reads happens."""

    def __init__(self, *, ok=True, bid=1.1000, positions=()):
        self.ok, self.bid, self.calls, self._positions = ok, bid, [], list(positions)

    def terminal_info(self):
        self.calls.append("terminal_info")
        return SimpleNamespace(connected=True) if self.ok else None

    def initialize(self):
        self.calls.append("initialize")
        return self.ok

    def last_error(self):
        return (-10005, "IPC timeout")

    def shutdown(self):
        self.calls.append("shutdown")

    def account_info(self):
        return SimpleNamespace(login=12345678)

    def symbol_select(self, s, flag):
        self.calls.append("symbol_select")
        return True

    def symbol_info(self, s):
        return SimpleNamespace(point=0.00001)

    def symbol_info_tick(self, s):
        return SimpleNamespace(bid=self.bid, ask=self.bid + 0.00002, time=1)

    def positions_get(self):
        return self._positions

    def orders_get(self):
        return []


class Collector:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)
        return True


async def no_state(state):
    return None


def test_mask_account_and_module_is_read_only():
    assert mask_account(12345678) == "*****678"
    source = (Path(__file__).parents[1] / "monitors" / "mt5_provider.py").read_text(encoding="utf-8")
    # No order placement/modification API is referenced anywhere in the provider.
    assert not re.search(r"order_send|order_check|order_calc|positions_close|Buy\(|Sell\(", source)


def test_mt5_states_are_truthful():
    c = Collector()
    missing = MT5Provider(["EURUSD"], c.publish, no_state, module_loader=lambda: None)
    assert missing.poll_once()["state"] is MT5State.NOT_INSTALLED
    down = MT5Provider(["EURUSD"], c.publish, no_state, module_loader=lambda: FakeMT5(ok=False))
    snap = down.poll_once()
    assert snap["state"] is MT5State.DISCONNECTED and "not reachable" in snap["detail"]

    class Boom(FakeMT5):
        def account_info(self):
            raise RuntimeError("x")

    err = MT5Provider(["EURUSD"], c.publish, no_state, module_loader=lambda: Boom())
    assert err.poll_once()["state"] is MT5State.ERROR


async def test_mt5_snapshot_masks_account_and_emits_only_meaningful_events():
    c, fake, t = Collector(), FakeMT5(), [0.0]
    provider = MT5Provider(["EURUSD"], c.publish, no_state, module_loader=lambda: fake, clock=lambda: t[0])
    for i in range(30):  # 30 flat ticks: no events
        t[0] = i
        await provider._apply(provider.poll_once())
    assert provider.state is MT5State.CONNECTED and provider.account == "*****678"
    assert provider.quotes["EURUSD"]["spread"] == 2.0
    assert c.events == []
    fake.bid = 1.1000 * 1.003  # +0.3% inside the window
    t[0] = 40
    await provider._apply(provider.poll_once())
    assert [e.kind for e in c.events] == ["price.move"]
    assert c.events[0].severity == 3 and c.events[0].symbol == "EURUSD"
    assert set(fake.calls) <= {"terminal_info", "initialize", "symbol_select", "shutdown"}


async def test_mt5_position_open_close_events():
    c, fake, t = Collector(), FakeMT5(), [0.0]
    provider = MT5Provider(["EURUSD"], c.publish, no_state, module_loader=lambda: fake, clock=lambda: t[0])
    await provider._apply(provider.poll_once())
    fake._positions = [SimpleNamespace(ticket=7, symbol="EURUSD", type=0, volume=0.1, price_open=1.1, profit=3.5)]
    await provider._apply(provider.poll_once())
    assert provider.floating_pnl == 3.5
    fake._positions = []
    await provider._apply(provider.poll_once())
    assert [e.kind for e in c.events] == ["position.opened", "position.closed"]


async def test_mt5_loop_survives_a_crashing_module():
    c = Collector()
    seen = []

    async def on_state(state):
        seen.append(state)

    class Crash:
        def terminal_info(self):
            raise OSError("terminal vanished")

    provider = MT5Provider(["EURUSD"], c.publish, on_state, module_loader=lambda: Crash(), poll_seconds=0.01)
    await provider.start()
    await asyncio.sleep(0.1)
    await provider.stop()
    assert seen and seen[0] is MT5State.ERROR


def test_faireconomy_parse_and_bad_rows():
    rows = [{"title": "CPI m/m", "country": "USD", "date": "2026-09-21T08:30:00-04:00", "impact": "High",
             "forecast": "0.3%", "previous": "0.2%"}, {"garbage": 1}]
    events = FaireconomyCalendarProvider.parse(rows)
    assert len(events) == 1 and events[0].importance == "High" and events[0].timestamp.utcoffset().total_seconds() == 0


class FixedCalendar(EconomicCalendarProvider):
    name = "fixed"

    def __init__(self, events):
        self.events = events

    async def fetch(self):
        return self.events


async def test_calendar_emits_upcoming_only_for_relevant_high_impact():
    now = datetime.now(UTC)
    mk = lambda i, cur, imp, m: CalendarEvent(id=i, currency=cur, event=f"E{i}", timestamp=now + timedelta(minutes=m), importance=imp)  # noqa: E731
    c = Collector()
    monitor = CalendarMonitor(FixedCalendar([mk("a", "USD", "High", 10), mk("b", "USD", "Low", 5),
                                             mk("c", "JPY", "High", 5), mk("d", "USD", "High", 90)]),
                              c.publish, no_state, ["EURUSD"])
    await monitor.tick()
    assert [e.dedupe_key for e in c.events] == ["a:calendar.upcoming"]
    assert c.events[0].severity == 3 and c.events[0].symbol == "EURUSD"
    assert monitor.snapshot()["state"] == "CONNECTED"


async def test_calendar_fetch_failure_is_reported_not_raised():
    class Bad(EconomicCalendarProvider):
        name = "bad"

        async def fetch(self):
            raise OSError("offline")

    monitor = CalendarMonitor(Bad(), Collector().publish, no_state, ["EURUSD"])
    await monitor.tick()
    assert monitor.snapshot()["state"] == "ERROR"


@pytest.fixture
async def manager():
    conn = await aiosqlite.connect(":memory:")
    runtime = Runtime()
    core = RealtimeEventCore(conn, runtime, None, gate=SignificanceGate(symbols=["EURUSD"], analyze=True))
    await core.start()
    got = []

    async def sub(payload):
        got.append(payload)

    core.subscribe(sub)

    class Store:
        async def get_latest(self):
            return None

    m = MonitorManager(core, Store(), mt5_enabled=True, calendar_enabled=False, symbols=["EURUSD"],
                       mt5_loader=lambda: None)
    yield m, core, runtime, got
    await m.stop()
    await core.close()
    await conn.close()


async def test_status_is_truthful_when_nothing_is_running(manager):
    m, *_ = manager
    m.mt5.state = MT5State.NOT_INSTALLED
    status = await m.status()
    assert status["mt5"]["state"] == "NOT INSTALLED" and status["mt5"]["read_only"] is True
    assert status["tradingview"]["state"] == "NOT FOUND"
    assert status["calendar"]["state"] == "DISABLED"
    assert status["quote"] is None and status["replay"] is False


async def test_demo_replay_flows_through_real_pipeline_and_is_labelled(manager, monkeypatch):
    m, core, runtime, got = manager
    real_sleep = asyncio.sleep

    async def fast(_):
        await real_sleep(0.01)

    monkeypatch.setattr("monitors.manager.asyncio.sleep", fast)
    await m.start_replay()
    assert (await m.status())["replay"] is True
    for _ in range(300):
        if len(runtime.calls) >= 3:
            break
        await real_sleep(0.02)
    status = await m.status()
    assert status["calendar"]["replay"] is True
    assert len(runtime.calls) == 3 and all(t.startswith("[DEMO REPLAY]") for t in runtime.calls)
    events = [g for g in got if g["type"] == "proactive_event"]
    assert all(e["event"]["payload"]["replay"] is True for e in events)
    assert {e["decision"] for e in events} == {EventDecision.ANALYZE.value}
    assert any(g["type"] == "event_analysis" for g in got)
