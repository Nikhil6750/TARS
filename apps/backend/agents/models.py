"""Bounded Agent framework — domain models.

An Agent is TARS's bounded, audited unit of autonomous-ish work: one
ON_DEMAND invocation, one SCHEDULED firing, or one CONTINUOUS iteration —
never an unbounded background process. This mirrors the same fail-closed,
audited philosophy `actions/plan_runtime.py` already applies to multi-step
action plans: every run is wrapped in a timeout, every run leaves a durable
`agent_runs` row (see storage/migrations/0002_tars_core_memory_agents.sql),
and "no strategy configured" or "action failed" are honest terminal outcomes
that get recorded, never silently upgraded to a fabricated success.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AgentMode(str, Enum):
    ON_DEMAND = "ON_DEMAND"
    SCHEDULED = "SCHEDULED"
    CONTINUOUS = "CONTINUOUS"


class AgentRunStatus(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class AgentConfig:
    """Bounds on how an Agent is allowed to run. `max_iterations=None` on a
    CONTINUOUS agent means "no fixed iteration count", not "unbounded and
    uncancellable" — AgentRuntime.stop_continuous() can always end the loop,
    and every individual iteration is still wrapped in `timeout_seconds`
    regardless of mode. ON_DEMAND agents always run exactly one iteration
    per invocation no matter what `max_iterations` is set to; the field only
    has meaning for CONTINUOUS agents.
    """

    mode: AgentMode
    interval_seconds: float | None = None
    timeout_seconds: float = 60.0
    max_iterations: int | None = None

    def __post_init__(self) -> None:
        if self.mode in (AgentMode.SCHEDULED, AgentMode.CONTINUOUS) and not self.interval_seconds:
            raise ValueError(f"{self.mode.value} agents require a positive interval_seconds")
        if self.interval_seconds is not None and self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0 if set")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if self.max_iterations is not None and self.max_iterations <= 0:
            raise ValueError("max_iterations must be > 0 if set")


@dataclass
class AgentRunResult:
    """What one bounded Agent.run_once() call produced. `data` is agent-
    specific structured detail (e.g. which symbols changed); it is never the
    place to smuggle in a fabricated status — `status`/`error` are the
    authoritative outcome fields AgentRuntime records to `agent_runs`."""

    status: AgentRunStatus
    summary: str
    error: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
