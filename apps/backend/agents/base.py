"""Bounded Agent base class + AgentRuntime.

`Agent.run_once()` is the single unit of bounded work a concrete agent
implements — one ON_DEMAND invocation, one SCHEDULED firing, or one
CONTINUOUS iteration. `AgentRuntime` is the only thing that actually calls
it: every call is timeout-bounded (`asyncio.wait_for`), concurrency-bounded
(a semaphore, so a burst of triggers waits rather than piling up
unboundedly), and audited to `agent_runs` via `AgentRunStore` — the same
resource-limited, fail-closed shape `actions/runtime.py`'s `ActionRuntime`
already applies to a single action, and `actions/plan_runtime.py`'s
`PlanRuntime` applies to a multi-step plan, adapted here to repeated agent
iterations.

Contract for concrete `run_once()` implementations, mirroring
`BaseSkill.execute()` vs. `SkillExecutionError` in `app/action_contracts.py`:
catch and return a FAILED `AgentRunResult` for *expected* failures (a
downstream action didn't succeed, a dependency is unavailable) — never
fabricate a SUCCEEDED result to paper over one. Let genuinely *unexpected*
exceptions propagate; `AgentRuntime._run_bounded` catches those, logs them,
records a FAILED run, and — critically for CONTINUOUS agents — keeps the
loop alive for the next iteration rather than letting one bad iteration kill
a long-running watcher.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from agents.models import AgentConfig, AgentMode, AgentRunResult, AgentRunStatus
from agents.store import AgentRunStore

logger = logging.getLogger("tars.agents")


class Agent(ABC):
    """Concrete agents set `name` as a class attribute (same pattern as
    `BaseSkill.name`) and receive their `AgentConfig` through `__init__`."""

    name: str

    def __init__(self, config: AgentConfig) -> None:
        self.config = config

    @abstractmethod
    async def run_once(self) -> AgentRunResult:
        """Perform one bounded unit of work and report an honest outcome.
        Must not raise for expected/handled failures — return a FAILED
        AgentRunResult instead. May raise for genuinely unexpected errors;
        AgentRuntime is responsible for catching those at the boundary."""


class AgentRuntime:
    def __init__(
        self,
        store: AgentRunStore,
        *,
        max_concurrent_runs: int = 3,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_concurrent_runs <= 0:
            raise ValueError("max_concurrent_runs must be positive")
        self.store = store
        self._semaphore = asyncio.Semaphore(max_concurrent_runs)
        self._clock = clock or (lambda: datetime.now(UTC))
        # CONTINUOUS-mode bookkeeping, keyed by agent.name so a given agent
        # can never have two concurrent continuous loops.
        self._continuous_tasks: dict[str, asyncio.Task[None]] = {}
        self._continuous_stop_events: dict[str, asyncio.Event] = {}

    async def run_on_demand(self, agent: Agent, *, trigger: str = "api") -> AgentRunResult:
        return await self._run_bounded(agent, trigger)

    def schedule(self, agent: Agent, scheduler: AsyncIOScheduler) -> str:
        """Registers an APScheduler interval job on the *caller-supplied*
        scheduler instance (app/scheduler.py's `build_scheduler` output) --
        this never constructs a second scheduler. Each firing runs through
        the same bounded/audited path as `run_on_demand`, with
        trigger="scheduler" so `agent_runs` distinguishes the two."""
        if agent.config.mode is not AgentMode.SCHEDULED:
            raise ValueError(f"{agent.name!r} is not configured for SCHEDULED mode")

        async def _job() -> None:
            await self._run_bounded(agent, "scheduler")

        job = scheduler.add_job(
            _job,
            "interval",
            seconds=agent.config.interval_seconds,
            id=f"agent:{agent.name}",
            replace_existing=True,
        )
        return job.id

    async def start_continuous(self, agent: Agent) -> None:
        if agent.config.mode is not AgentMode.CONTINUOUS:
            raise ValueError(f"{agent.name!r} is not configured for CONTINUOUS mode")
        if agent.name in self._continuous_tasks:
            raise ValueError(f"{agent.name!r} is already running continuously")
        stop_event = asyncio.Event()
        self._continuous_stop_events[agent.name] = stop_event
        task = asyncio.create_task(
            self._continuous_loop(agent, stop_event), name=f"agent-continuous:{agent.name}"
        )
        self._continuous_tasks[agent.name] = task

    async def stop_continuous(self, agent_name: str) -> None:
        """Actually waits for the loop to unwind -- not fire-and-forget.
        A no-op (not an error) if the named agent isn't running, matching
        MemoryService.forget()'s "idempotent, not an error" convention for
        stopping something that may have already stopped on its own (e.g.
        it hit max_iterations first)."""
        task = self._continuous_tasks.get(agent_name)
        if task is None:
            return
        stop_event = self._continuous_stop_events.get(agent_name)
        if stop_event is not None:
            stop_event.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._continuous_tasks.pop(agent_name, None)
            self._continuous_stop_events.pop(agent_name, None)

    async def get_recent_runs(
        self, agent_name: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        return await self.store.list_recent(agent_name, limit)

    async def _continuous_loop(self, agent: Agent, stop_event: asyncio.Event) -> None:
        iterations = 0
        max_iterations = agent.config.max_iterations
        interval = agent.config.interval_seconds or 0.0
        while not stop_event.is_set():
            if max_iterations is not None and iterations >= max_iterations:
                return
            await self._run_bounded(agent, "continuous_loop")
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                return
            # Sleep *between* iterations rather than looping back-to-back --
            # waiting on the stop event (with a timeout) means a stop()
            # during the sleep wakes the loop immediately instead of at the
            # next full interval boundary.
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def _run_bounded(self, agent: Agent, trigger: str) -> AgentRunResult:
        async with self._semaphore:
            run_id = str(uuid.uuid4())
            started = self._now()
            await self.store.start_run(run_id, agent.name, agent.config.mode, trigger, started)
            try:
                result = await asyncio.wait_for(
                    agent.run_once(), timeout=agent.config.timeout_seconds
                )
                await self.store.finish_run(
                    run_id,
                    result.status,
                    self._now(),
                    iterations=1,
                    summary=result.summary,
                    error=result.error,
                )
                return result
            except TimeoutError:
                error = f"Agent iteration exceeded {agent.config.timeout_seconds}s timeout"
                await self.store.finish_run(
                    run_id,
                    AgentRunStatus.TIMED_OUT,
                    self._now(),
                    iterations=1,
                    summary="Agent iteration timed out",
                    error=error,
                )
                return AgentRunResult(
                    status=AgentRunStatus.TIMED_OUT,
                    summary="Agent iteration timed out",
                    error=error,
                )
            except Exception as exc:
                # Unexpected failure -- logged and recorded, but deliberately
                # not re-raised: a CONTINUOUS loop must survive one bad
                # iteration rather than dying silently mid-watch.
                logger.exception("agent %r run %s failed unexpectedly", agent.name, run_id)
                error = f"{type(exc).__name__}: {exc}"
                await self.store.finish_run(
                    run_id,
                    AgentRunStatus.FAILED,
                    self._now(),
                    iterations=1,
                    summary="Agent iteration failed unexpectedly",
                    error=error,
                )
                return AgentRunResult(
                    status=AgentRunStatus.FAILED,
                    summary="Agent iteration failed unexpectedly",
                    error=error,
                )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("Agent runtime clock must return a timezone-aware datetime")
        return now.astimezone(UTC)
