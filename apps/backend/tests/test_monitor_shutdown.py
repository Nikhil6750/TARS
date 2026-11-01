"""Stopping the network monitors.

Cancelling a fetch in the same loop iteration as its HTTP timeout comes back as a
timeout, not a cancellation. A monitor handles timeouts by trying again later, so
it used to carry on running with `stop()` waiting on it forever. These reproduce
that exchange directly rather than aiming at a timer.
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import anyio
import httpx
import pytest

from monitors import calendar as calendar_module
from monitors.calendar import CalendarMonitor, EconomicCalendarProvider, stop_monitor_task
from monitors.manager import MonitorManager
from monitors.news import NewsMonitor, NewsProvider, RssNewsProvider

# Long enough to tell "returned" from "never returns", short enough to fail fast.
STOP_LIMIT = 2.0


async def noop(*_args):
    return None


class Fetch:
    """What the monitors call, as either provider."""

    name = "test"

    def __init__(self):
        self.calls = 0
        self.entered = asyncio.Event()

    async def latest(self, _symbols):
        return await self._fetch()

    async def fetch(self):
        return await self._fetch()

    async def _fetch(self):
        self.calls += 1
        self.entered.set()
        return await self.body()


class LosesCancellation(Fetch):
    async def body(self):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise httpx.ReadTimeout("timed out") from None


class RealTimeoutScope(Fetch):
    """The race itself: the real anyio timeout httpcore wraps around every read,
    expiring as the task is cancelled. `expire()` then `stop()` run in one step, so
    they land in the same loop iteration every time."""

    async def body(self):
        try:
            with anyio.fail_after(60) as self.scope:
                await asyncio.Event().wait()
        except TimeoutError:
            raise httpx.ReadTimeout("timed out") from None

    def expire(self):
        self.scope.deadline = anyio.current_time() - 1
        self.scope.cancel()


class IgnoresCancellation(Fetch):
    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()

    async def body(self):
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                pass
        return []


def news(provider, **kwargs):
    return NewsMonitor(provider, noop, noop, ["EURUSD"], **kwargs)


def calendar(provider, **kwargs):
    return CalendarMonitor(provider, noop, noop, ["EURUSD"], **kwargs)


MONITORS = [pytest.param(news, id="news"), pytest.param(calendar, id="calendar")]


async def started(monitor, provider):
    await monitor.start()
    await asyncio.wait_for(provider.entered.wait(), 2)
    return monitor._task


@pytest.mark.parametrize("build", MONITORS)
async def test_stop_ends_a_monitor_whose_cancellation_became_a_timeout(build):
    provider = LosesCancellation()
    monitor = build(provider)
    task = await started(monitor, provider)
    await asyncio.wait_for(monitor.stop(), STOP_LIMIT)
    assert task.done()
    assert monitor.state == "ERROR", "the race did not happen"
    assert provider.calls == 1, "it fetched again after being stopped"


@pytest.mark.parametrize("build", MONITORS)
async def test_stop_survives_the_race_through_a_real_timeout_scope(build):
    provider = RealTimeoutScope()
    monitor = build(provider)
    task = await started(monitor, provider)
    provider.expire()
    await asyncio.wait_for(monitor.stop(), STOP_LIMIT)
    assert task.done()


async def test_the_http_client_is_closed_when_the_cancellation_is_lost(monkeypatch):
    clients = []
    entered = asyncio.Event()

    async def respond(_request):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise httpx.ReadTimeout("timed out") from None

    real = httpx.AsyncClient

    def recorded(**kwargs):
        clients.append(real(transport=httpx.MockTransport(respond), **kwargs))
        return clients[-1]

    monkeypatch.setattr(httpx, "AsyncClient", recorded)
    monitor = news(RssNewsProvider("http://feed.test/rss"))
    await monitor.start()
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(monitor.stop(), STOP_LIMIT)
    assert monitor.state == "ERROR"
    assert [client.is_closed for client in clients] == [True]


@pytest.mark.parametrize("build", MONITORS)
async def test_stop_is_bounded_when_a_task_will_not_end(build, monkeypatch, caplog):
    monkeypatch.setattr(calendar_module, "MONITOR_STOP_SECONDS", 0.05)
    provider = IgnoresCancellation()
    monitor = build(provider)
    task = await started(monitor, provider)
    began = time.monotonic()
    with caplog.at_level(logging.ERROR):
        await asyncio.wait_for(monitor.stop(), STOP_LIMIT)
    try:
        assert time.monotonic() - began < 1
        assert not task.done()
        assert "did not stop" in caplog.text
    finally:
        provider.release.set()
        await asyncio.wait_for(task, 2)


async def test_stop_cancels_a_stuck_task_a_fixed_number_of_times(monkeypatch):
    monkeypatch.setattr(calendar_module, "MONITOR_STOP_SECONDS", 0.05)
    cancels = []

    async def stubborn():
        while True:
            try:
                await asyncio.sleep(0.5)
                return
            except asyncio.CancelledError:
                cancels.append(1)

    task = asyncio.create_task(stubborn())
    await asyncio.sleep(0)
    await stop_monitor_task(task, "test")
    await asyncio.sleep(0)
    assert len(cancels) == 2
    await asyncio.wait_for(task, 2)


async def test_the_manager_stops_with_both_monitors_in_the_race():
    core = SimpleNamespace(publish=noop, set_provider_status=noop)
    feed, dates = LosesCancellation(), LosesCancellation()
    manager = MonitorManager(core, None, mt5_enabled=False, calendar_provider=dates, news_provider=feed)
    await manager.start()
    await asyncio.wait_for(asyncio.gather(feed.entered.wait(), dates.entered.wait()), 2)
    await asyncio.wait_for(manager.stop(), STOP_LIMIT)
    assert manager.news._task.done() and manager.calendar._task.done()


# ---- nothing else changed ------------------------------------------------


class Answers(Fetch):
    def __init__(self):
        super().__init__()
        self.failing = False

    async def body(self):
        if self.failing:
            raise httpx.ConnectError("no route")
        return []


@pytest.mark.parametrize("build", MONITORS)
async def test_a_failed_fetch_still_leaves_the_monitor_running(build):
    provider = Answers()
    provider.failing = True
    monitor = build(provider, tick_seconds=0.001)
    await monitor.start()
    deadline = time.monotonic() + 3
    while provider.calls < 3 and time.monotonic() < deadline:
        await asyncio.sleep(0.001)
    try:
        assert provider.calls >= 3
        assert not monitor._task.done()
        assert monitor.state == "ERROR"
    finally:
        await monitor.stop()


@pytest.mark.parametrize("build", MONITORS)
async def test_a_stopped_monitor_runs_normally_when_started_again(build):
    provider = Answers()
    provider.failing = True  # the calendar only fetches every tick until one succeeds
    monitor = build(provider, tick_seconds=0.001)
    await started(monitor, provider)
    await asyncio.wait_for(monitor.stop(), STOP_LIMIT)
    before = provider.calls
    await monitor.start()
    deadline = time.monotonic() + 3
    while provider.calls < before + 3 and time.monotonic() < deadline:
        await asyncio.sleep(0.001)
    try:
        assert provider.calls >= before + 3, "it stopped after one tick"
        assert not monitor._task.done()
    finally:
        await asyncio.wait_for(monitor.stop(), STOP_LIMIT)


async def test_stopping_during_the_sleep_between_ticks_is_immediate():
    provider = Answers()
    monitor = news(provider, tick_seconds=999)
    task = await started(monitor, provider)
    await asyncio.sleep(0.01)
    began = time.monotonic()
    await monitor.stop()
    assert task.cancelled()
    assert time.monotonic() - began < 0.5


async def test_stopping_a_monitor_that_never_started_does_nothing():
    await news(Answers()).stop()
    await calendar(Answers()).stop()


def test_the_real_providers_still_implement_the_interfaces():
    assert issubclass(RssNewsProvider, NewsProvider)
    assert issubclass(calendar_module.FaireconomyCalendarProvider, EconomicCalendarProvider)
