from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from agents.models import AgentRunResult, AgentRunStatus
from events.core import (
    EventDecision,
    EventSource,
    NormalizedEvent,
    ProviderStatus,
    RealtimeEventCore,
    SignificanceGate,
)


class Runtime:
    def __init__(self):
        self.calls = []

    async def run_on_demand(self, agent, **kwargs):
        self.calls.append(agent)
        return AgentRunResult(status=AgentRunStatus.SUCCEEDED, summary="Evidence unavailable.")


def event(**kwargs):
    return NormalizedEvent(source=EventSource.SYSTEM, kind="test", severity=2,
                           title="Monitor alert", dedupe_key=kwargs.pop("dedupe_key", "test"), **kwargs)


@pytest.fixture
async def core():
    conn = await aiosqlite.connect(":memory:")
    runtime = Runtime()
    instance = RealtimeEventCore(conn, runtime, None)
    await instance.start()
    yield instance, runtime
    await instance.close()
    await conn.close()


async def test_concurrent_duplicate_events_admitted_once(core):
    instance, _ = core
    same = event()
    results = await asyncio.gather(*(instance.publish(same) for _ in range(20)))
    assert sum(results) == 1
    assert len(instance.recent) == 1


async def test_cooldown_uses_receiver_clock_and_priority_can_escalate(core):
    instance, _ = core
    now = datetime.now(UTC)
    instance.clock = lambda: now
    assert await instance.publish(event())
    assert not await instance.publish(event())
    critical = event().model_copy(update={"severity": 4})
    assert await instance.publish(critical)
    now += timedelta(seconds=31)
    assert await instance.publish(event())


async def test_disconnect_and_bad_subscriber_do_not_break_delivery(core):
    instance, _ = core
    received = []
    async def bad(_payload):
        raise RuntimeError("disconnected")
    async def good(payload):
        received.append(payload)
    instance.subscribe(bad)
    instance.subscribe(good)
    await instance.set_provider_status(EventSource.MT5, ProviderStatus.DISCONNECTED)
    assert await instance.publish(event())
    await instance._queue.join()
    assert any(p["type"] == "proactive_event" for p in received)
    assert instance.providers["MT5"] == "DISCONNECTED"


@pytest.mark.parametrize(("severity", "symbol", "expected"), [
    (0, "EURUSD", EventDecision.IGNORE), (1, "EURUSD", EventDecision.IGNORE),
    (2, "EURUSD", EventDecision.NOTIFY), (3, "EURUSD", EventDecision.ANALYZE),
    (4, "EURUSD", EventDecision.SPEAK), (4, "GBPUSD", EventDecision.IGNORE),
])
def test_significance_severity_and_user_relevance(severity, symbol, expected):
    gate = SignificanceGate(symbols=["EURUSD"], analyze=True, speak=True)
    assert gate.decide(event(symbol=symbol).model_copy(update={"severity": severity})) == expected


async def test_only_meaningful_events_escalate_via_existing_bounded_runtime(core):
    instance, runtime = core
    instance.gate = SignificanceGate(analyze=True)
    for i, severity in enumerate([0, 1, 2, 3]):
        await instance.publish(event(dedupe_key=str(i)).model_copy(update={"severity": severity}))
    await instance._queue.join()
    assert len(runtime.calls) == 1
    assert runtime.calls[0].config.timeout_seconds == 45


async def test_recent_event_persistence_and_dedupe_survive_restart(tmp_path):
    conn = await aiosqlite.connect(tmp_path / "events.db")
    first = RealtimeEventCore(conn, Runtime(), None)
    await first.start()
    same = event()
    await first.publish(same)
    await first.close()
    second = RealtimeEventCore(conn, Runtime(), None)
    await second.start()
    assert len(second.recent) == 1
    assert not await second.publish(same)
    await second.close()
    await conn.close()


async def test_expired_event_never_escalates(core):
    instance, runtime = core
    assert not await instance.publish(event(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert not runtime.calls
