from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentMode(str, Enum):
    ON_DEMAND = "ON_DEMAND"
    SCHEDULED = "SCHEDULED"
    CONTINUOUS = "CONTINUOUS"


class AgentStatus(str, Enum):
    READY = "READY"
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    EXHAUSTED = "EXHAUSTED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


TERMINAL_AGENT_STATUSES = {
    AgentStatus.CANCELLED,
    AgentStatus.SUCCEEDED,
    AgentStatus.FAILED,
    AgentStatus.TIMED_OUT,
    AgentStatus.EXHAUSTED,
}


class DecisionKind(str, Enum):
    ACTION = "ACTION"
    COMPLETE = "COMPLETE"
    WAIT = "WAIT"


class StrategyAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    PROVIDER_FAILED = "PROVIDER_FAILED"


class RuntimeLimits(StrictModel):
    max_iterations_per_run: int = Field(default=8, ge=1, le=100)
    max_cycles: int = Field(default=1, ge=1, le=1000)
    max_provider_retries: int = Field(default=2, ge=0, le=10)
    provider_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    action_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    run_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)


class AgentDefinition(StrictModel):
    agent_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = Field(min_length=1, max_length=200)
    mode: AgentMode
    intelligence_provider: str = Field(
        min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$"
    )
    strategy_id: str | None = Field(default=None, min_length=1, max_length=200)
    interval_seconds: float | None = Field(default=None, gt=0, le=86400)
    limits: RuntimeLimits = Field(default_factory=RuntimeLimits)

    @model_validator(mode="after")
    def validate_mode(self) -> AgentDefinition:
        if self.mode in {AgentMode.SCHEDULED, AgentMode.CONTINUOUS}:
            if self.interval_seconds is None:
                raise ValueError("scheduled and continuous agents require interval_seconds")
        elif self.interval_seconds is not None:
            raise ValueError("on-demand agents cannot define interval_seconds")
        if self.mode != AgentMode.CONTINUOUS and self.limits.max_cycles != 1:
            raise ValueError("only continuous agents may define more than one cycle")
        return self


class MemoryItem(StrictModel):
    source_id: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=8000)
    source_type: str = Field(min_length=1, max_length=50)


class MemoryContext(StrictModel):
    items: tuple[MemoryItem, ...] = Field(default_factory=tuple, max_length=50)


class SkillDescriptor(StrictModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    capabilities: tuple[str, ...] = Field(default_factory=tuple, max_length=100)


class SkillCall(StrictModel):
    """Untrusted data proposal. It deliberately carries no risk or authority."""

    skill: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    action: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    arguments: dict[str, Any] = Field(default_factory=dict)


class OrchestratorDecision(StrictModel):
    kind: DecisionKind
    summary: str = Field(min_length=1, max_length=2000)
    skill_call: SkillCall | None = None
    output: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_shape(self) -> OrchestratorDecision:
        if self.kind == DecisionKind.ACTION and self.skill_call is None:
            raise ValueError("ACTION decisions require skill_call")
        if self.kind != DecisionKind.ACTION and self.skill_call is not None:
            raise ValueError("only ACTION decisions may include skill_call")
        return self


class StrategyDefinition(StrictModel):
    strategy_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    source: str = Field(default="quant_brain", pattern=r"^quant_brain$")
    description: str | None = Field(default=None, max_length=4000)


class StrategySignal(StrictModel):
    signal_id: str = Field(min_length=1, max_length=200)
    strategy_id: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any]
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    source: str = Field(default="quant_brain", pattern=r"^quant_brain$")


class StrategyContext(StrictModel):
    availability: StrategyAvailability
    definition: StrategyDefinition | None = None
    signals: tuple[StrategySignal, ...] = Field(default_factory=tuple)
    error: str | None = None

    @model_validator(mode="after")
    def enforce_boundary(self) -> StrategyContext:
        if self.availability != StrategyAvailability.AVAILABLE:
            if self.definition is not None or self.signals:
                raise ValueError("unavailable strategy context cannot contain signals")
        elif self.definition is None:
            raise ValueError("available strategy context requires a definition")
        return self


class IntelligenceRequest(StrictModel):
    job_id: UUID
    objective: str
    iteration: int = Field(ge=1)
    skills: tuple[SkillDescriptor, ...]
    memory: MemoryContext
    strategy: StrategyContext
    last_action_result: dict[str, Any] | None = None


class AgentJob(StrictModel):
    job_id: UUID = Field(default_factory=uuid4)
    dedupe_key: str = Field(min_length=1, max_length=200)
    definition: AgentDefinition
    objective: str = Field(min_length=1, max_length=8000)
    memory: MemoryContext = Field(default_factory=MemoryContext)
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    scheduled_for: datetime | None = None

    @model_validator(mode="after")
    def validate_schedule(self) -> AgentJob:
        if self.definition.mode == AgentMode.SCHEDULED and self.scheduled_for is None:
            raise ValueError("scheduled jobs require scheduled_for")
        if self.definition.mode != AgentMode.SCHEDULED and self.scheduled_for is not None:
            raise ValueError("scheduled_for is only valid for scheduled jobs")
        return self


class AgentRun(StrictModel):
    job_id: UUID
    status: AgentStatus
    iteration: int = Field(ge=0)
    cycle: int = Field(ge=0)
    summary: str
    next_run_at: datetime | None = None
    pending_action_id: UUID | None = None
    error: str | None = None
    updated_at: datetime


@runtime_checkable
class IntelligenceProvider(Protocol):
    name: str

    async def decide(self, request: IntelligenceRequest) -> OrchestratorDecision: ...


@runtime_checkable
class SkillDiscoveryProvider(Protocol):
    async def discover(self) -> tuple[SkillDescriptor, ...]: ...


@runtime_checkable
class StrategyProvider(Protocol):
    name: str

    async def get_definition(self, strategy_id: str) -> StrategyDefinition: ...

    async def get_signals(
        self, definition: StrategyDefinition, *, job_id: UUID
    ) -> tuple[StrategySignal, ...]: ...
