from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.action_contracts import RiskLevel


class PlanStatus(str, Enum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    WAITING_OBSERVATION = "WAITING_OBSERVATION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class StepStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    WAITING_OBSERVATION = "WAITING_OBSERVATION"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


class VerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class ObservationSource(str, Enum):
    WINDOWS_UI_AUTOMATION = "WINDOWS_UI_AUTOMATION"
    BROWSER = "BROWSER"
    NATIVE_VISION = "NATIVE_VISION"


class PlanProvenance(str, Enum):
    API = "API"
    ASSISTANT = "ASSISTANT"
    DETERMINISTIC = "DETERMINISTIC"


class RecoveryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_retry: bool = False
    allow_reobserve: bool = False
    alternate_arguments: list[dict[str, Any]] = Field(default_factory=list, max_length=2)


class ActionStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: UUID = Field(default_factory=uuid4)
    skill: str = Field(min_length=1, max_length=100)
    action: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)
    expected_result: dict[str, Any] = Field(default_factory=dict)
    # A proposal may carry a hint for display, but the runtime always replaces it
    # with PermissionEngine.classify() before making an execution decision.
    risk_level: RiskLevel | None = None
    status: StepStatus = StepStatus.PENDING
    dependencies: list[UUID] = Field(default_factory=list, max_length=32)
    recovery: RecoveryPolicy = Field(default_factory=RecoveryPolicy)


class ActionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: UUID = Field(default_factory=uuid4)
    goal: str = Field(min_length=1, max_length=1000)
    context: dict[str, Any] = Field(default_factory=dict)
    steps: list[ActionStep] = Field(min_length=1)
    status: PlanStatus = PlanStatus.PLANNED
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    provenance: PlanProvenance = PlanProvenance.API

    @model_validator(mode="after")
    def validate_graph(self) -> ActionPlan:
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("Action plan contains duplicate step IDs")
        positions = {step_id: index for index, step_id in enumerate(step_ids)}
        for index, step in enumerate(self.steps):
            if self.status == PlanStatus.PLANNED and step.status != StepStatus.PENDING:
                raise ValueError("New action plan steps must have PENDING status")
            if step.step_id in step.dependencies:
                raise ValueError("Action step cannot depend on itself")
            if len(step.dependencies) != len(set(step.dependencies)):
                raise ValueError("Action step contains duplicate dependencies")
            for dependency in step.dependencies:
                if dependency not in positions:
                    raise ValueError("Action step dependency is not in the plan")
                if positions[dependency] >= index:
                    raise ValueError("Action step dependencies must refer to earlier steps")
        return self


class StructuredObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_id: UUID = Field(default_factory=uuid4)
    plan_id: UUID
    step_id: UUID
    request_id: UUID
    source: ObservationSource
    state: dict[str, Any]
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class VerificationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_id: UUID
    expected_state: dict[str, Any]
    observed_state: dict[str, Any]
    status: VerificationStatus
    reason: str
    verified_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PlanExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: ActionPlan
    current_step_id: UUID | None = None
    active_request_id: UUID | None = None
    pending_operation: dict[str, Any] | None = None
    verification: VerificationRecord | None = None
    error: str | None = None


TERMINAL_PLAN_STATUSES = {
    PlanStatus.COMPLETED,
    PlanStatus.FAILED,
    PlanStatus.BLOCKED,
    PlanStatus.CANCELLED,
    PlanStatus.TIMED_OUT,
}
