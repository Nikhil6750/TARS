from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from agents.base import Agent, AgentRuntime
from agents.models import AgentConfig, AgentMode, AgentRunResult, AgentRunStatus
from agents.store import AgentRunStore
from storage.migrator import run_migrations


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "agent_runtime_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


@pytest.fixture
def store(conn):
    return AgentRunStore(conn)


@pytest.fixture
def runtime(store):
    return AgentRuntime(store, max_concurrent_runs=3)


class ScriptedAgent(Agent):
    """A trivial Agent whose run_once() behavior is scripted by the test,
    same spirit as test_plan_runtime.py's PlanSkill fake."""

    name = "scripted_agent"

    def __init__(self, config: AgentConfig, *, behavior: str = "succeed", delay: float = 0.0) -> None:
        super().__init__(config)
        self.behavior = behavior
        self.delay = delay
        self.call_count = 0

    async def run_once(self) -> AgentRunResult:
        self.call_count += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.behavior == "succeed":
            return AgentRunResult(status=AgentRunStatus.SUCCEEDED, summary="ok")
        if self.behavior == "fail":
            return AgentRunResult(status=AgentRunStatus.FAILED, summary="failed", error="boom")
        if self.behavior == "hang":
            await asyncio.sleep(10)
            return AgentRunResult(status=AgentRunStatus.SUCCEEDED, summary="should not get here")
        if self.behavior == "raise":
            raise RuntimeError("unexpected boom")
        raise AssertionError(f"unknown behavior {self.behavior!r}")


# ---- AgentConfig validation -------------------------------------------------


def test_agent_config_on_demand_defaults_are_valid():
    config = AgentConfig(mode=AgentMode.ON_DEMAND)
    assert config.interval_seconds is None
    assert config.timeout_seconds == 60.0
    assert config.max_iterations is None


def test_agent_config_scheduled_requires_interval():
    with pytest.raises(ValueError):
        AgentConfig(mode=AgentMode.SCHEDULED)


def test_agent_config_continuous_requires_interval():
    with pytest.raises(ValueError):
        AgentConfig(mode=AgentMode.CONTINUOUS)


def test_agent_config_rejects_non_positive_interval():
    with pytest.raises(ValueError):
        AgentConfig(mode=AgentMode.SCHEDULED, interval_seconds=0)
    with pytest.raises(ValueError):
        AgentConfig(mode=AgentMode.SCHEDULED, interval_seconds=-5)


def test_agent_config_rejects_non_positive_timeout():
    with pytest.raises(ValueError):
        AgentConfig(mode=AgentMode.ON_DEMAND, timeout_seconds=0)


def test_agent_config_rejects_non_positive_max_iterations():
    with pytest.raises(ValueError):
        AgentConfig(mode=AgentMode.CONTINUOUS, interval_seconds=1, max_iterations=0)


def test_agent_config_accepts_valid_continuous_config():
    config = AgentConfig(mode=AgentMode.CONTINUOUS, interval_seconds=30, max_iterations=10)
    assert config.max_iterations == 10


# ---- run_on_demand -----------------------------------------------------------


async def test_run_on_demand_happy_path(runtime):
    agent = ScriptedAgent(AgentConfig(mode=AgentMode.ON_DEMAND, timeout_seconds=5), behavior="succeed")

    result = await runtime.run_on_demand(agent)

    assert result.status == AgentRunStatus.SUCCEEDED
    runs = await runtime.get_recent_runs(agent.name)
    assert len(runs) == 1
    assert runs[0]["status"] == "SUCCEEDED"
    assert runs[0]["trigger"] == "api"
    assert runs[0]["mode"] == "ON_DEMAND"
    assert runs[0]["finished_at"] is not None


async def test_run_on_demand_timeout_is_recorded_not_raised(runtime):
    agent = ScriptedAgent(AgentConfig(mode=AgentMode.ON_DEMAND, timeout_seconds=0.05), behavior="hang")

    result = await runtime.run_on_demand(agent)

    assert result.status == AgentRunStatus.TIMED_OUT
    runs = await runtime.get_recent_runs(agent.name)
    assert runs[0]["status"] == "TIMED_OUT"
    assert runs[0]["error"] is not None


async def test_run_on_demand_failure_path(runtime):
    agent = ScriptedAgent(AgentConfig(mode=AgentMode.ON_DEMAND, timeout_seconds=5), behavior="fail")

    result = await runtime.run_on_demand(agent)

    assert result.status == AgentRunStatus.FAILED
    assert result.error == "boom"
    runs = await runtime.get_recent_runs(agent.name)
    assert runs[0]["status"] == "FAILED"
    assert runs[0]["error"] == "boom"


async def test_run_on_demand_unexpected_exception_is_caught_and_recorded(runtime):
    agent = ScriptedAgent(AgentConfig(mode=AgentMode.ON_DEMAND, timeout_seconds=5), behavior="raise")

    result = await runtime.run_on_demand(agent)

    assert result.status == AgentRunStatus.FAILED
    assert "unexpected boom" in result.error
    runs = await runtime.get_recent_runs(agent.name)
    assert runs[0]["status"] == "FAILED"


# ---- concurrency bound --------------------------------------------------------


async def test_semaphore_bounds_concurrent_runs(store):
    runtime = AgentRuntime(store, max_concurrent_runs=2)
    concurrent = 0
    max_concurrent = 0
    lock = asyncio.Lock()

    class TrackingAgent(Agent):
        name = "tracking_agent"

        async def run_once(self) -> AgentRunResult:
            nonlocal concurrent, max_concurrent
            async with lock:
                concurrent += 1
                max_concurrent = max(max_concurrent, concurrent)
            await asyncio.sleep(0.08)
            async with lock:
                concurrent -= 1
            return AgentRunResult(status=AgentRunStatus.SUCCEEDED, summary="ok")

    agent = TrackingAgent(AgentConfig(mode=AgentMode.ON_DEMAND, timeout_seconds=5))

    await asyncio.gather(*(runtime.run_on_demand(agent) for _ in range(5)))

    assert max_concurrent <= 2
    runs = await runtime.get_recent_runs(agent.name)
    assert len(runs) == 5
    assert all(r["status"] == "SUCCEEDED" for r in runs)


# ---- continuous lifecycle ------------------------------------------------------


async def test_continuous_agent_stops_itself_at_max_iterations(store):
    runtime = AgentRuntime(store, max_concurrent_runs=3)
    agent = ScriptedAgent(
        AgentConfig(mode=AgentMode.CONTINUOUS, interval_seconds=0.01, timeout_seconds=1, max_iterations=3),
        behavior="succeed",
    )

    await runtime.start_continuous(agent)
    await asyncio.sleep(0.5)

    assert agent.call_count == 3
    runs = await runtime.get_recent_runs(agent.name)
    assert len(runs) == 3
    assert all(r["status"] == "SUCCEEDED" for r in runs)
    assert all(r["trigger"] == "continuous_loop" for r in runs)

    # The loop already finished on its own; stop_continuous should still be
    # a clean, awaitable no-op rather than erroring on a dead task.
    await runtime.stop_continuous(agent.name)


async def test_continuous_agent_stops_cleanly_before_max_iterations(store):
    runtime = AgentRuntime(store, max_concurrent_runs=3)
    agent = ScriptedAgent(
        AgentConfig(mode=AgentMode.CONTINUOUS, interval_seconds=0.05, timeout_seconds=1),
        behavior="succeed",
    )

    await runtime.start_continuous(agent)
    await asyncio.sleep(0.12)
    await runtime.stop_continuous(agent.name)
    count_after_stop = agent.call_count
    await asyncio.sleep(0.2)

    assert agent.call_count == count_after_stop  # no further iterations after stop()


async def test_start_continuous_rejects_duplicate_agent_name(store):
    runtime = AgentRuntime(store, max_concurrent_runs=3)
    agent = ScriptedAgent(
        AgentConfig(mode=AgentMode.CONTINUOUS, interval_seconds=1, timeout_seconds=1), behavior="succeed"
    )

    await runtime.start_continuous(agent)
    try:
        with pytest.raises(ValueError):
            await runtime.start_continuous(agent)
    finally:
        await runtime.stop_continuous(agent.name)


async def test_start_continuous_rejects_wrong_mode(store):
    runtime = AgentRuntime(store, max_concurrent_runs=3)
    agent = ScriptedAgent(AgentConfig(mode=AgentMode.ON_DEMAND), behavior="succeed")

    with pytest.raises(ValueError):
        await runtime.start_continuous(agent)


async def test_stop_continuous_on_unknown_agent_is_a_noop(store):
    runtime = AgentRuntime(store, max_concurrent_runs=3)
    await runtime.stop_continuous("never_started")  # must not raise


# ---- scheduling ------------------------------------------------------------------


async def test_schedule_rejects_wrong_mode(store):
    runtime = AgentRuntime(store, max_concurrent_runs=3)
    agent = ScriptedAgent(AgentConfig(mode=AgentMode.ON_DEMAND), behavior="succeed")
    scheduler = AsyncIOScheduler()

    with pytest.raises(ValueError):
        runtime.schedule(agent, scheduler)


async def test_schedule_registers_an_interval_job_on_the_given_scheduler(store):
    runtime = AgentRuntime(store, max_concurrent_runs=3)
    agent = ScriptedAgent(
        AgentConfig(mode=AgentMode.SCHEDULED, interval_seconds=60, timeout_seconds=5), behavior="succeed"
    )
    scheduler = AsyncIOScheduler()

    job_id = runtime.schedule(agent, scheduler)

    job = scheduler.get_job(job_id)
    assert job is not None
    assert job.trigger.interval.total_seconds() == 60
