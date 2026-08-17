"""Wave 2A shared action/skill contracts, mirroring
contracts/action-request.schema.json and contracts/action-result.schema.json
for typed Python use across the action runtime and skill implementations.

Same pattern as app.schemas for the V1 trading/assistant contracts: this
module is a typed convenience layer; the canonical validation is
`app.contracts.validate_action_request` / `validate_action_result`, run
against the frozen schema files themselves, so this module cannot silently
drift into accepting something the contract forbids.

This module is shared across Wave 2A ownership boundaries (Claude Code's
skills, Codex's action runtime/permission engine, Antigravity's native
shell/HUD client code) -- see docs/coordination/wave2/M2A_INTERFACES.md.
Treat changes as breaking-change events requiring a schema_version bump and
coordinator sign-off, same as the V1 contracts.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class RiskLevel(str, Enum):
    """Classification a Skill assigns to a requested action. Enforcement is
    the permission engine's job (Codex, action runtime); a Skill only
    classifies, it never enforces its own classification -- the LLM has no
    path to bypass enforcement by having a skill under-report risk, because
    the permission engine re-derives/validates the classification rather
    than trusting it blindly (see M2A_SPEC.md requirement 9)."""

    READ_ONLY = "READ_ONLY"
    LOW_RISK = "LOW_RISK"
    CONFIRM_REQUIRED = "CONFIRM_REQUIRED"
    BLOCKED = "BLOCKED"


class ActionSource(str, Enum):
    hud = "hud"
    voice_ptt = "voice_ptt"
    voice_wake_word = "voice_wake_word"
    hotkey = "hotkey"
    deterministic = "deterministic"
    api = "api"


class ActionStatus(str, Enum):
    PENDING = "PENDING"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    DENIED = "DENIED"
    BLOCKED = "BLOCKED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


TERMINAL_STATUSES = {
    ActionStatus.DENIED,
    ActionStatus.BLOCKED,
    ActionStatus.SUCCEEDED,
    ActionStatus.FAILED,
}


class WindowBounds(BaseModel):
    x: int
    y: int
    width: int
    height: int


class WindowState(str, Enum):
    normal = "normal"
    minimized = "minimized"
    maximized = "maximized"
    unknown = "unknown"


class ContextSource(str, Enum):
    """Capture mechanism that produced an ActiveWindowContext -- distinct
    from ActionSource, which is what originated the *action request*."""

    win32 = "win32"
    ui_automation = "ui_automation"


class MonitorInfo(BaseModel):
    """Geometry of the display containing a window -- no pixel/screenshot
    data, per M2A_SPEC.md's no-content-capture rule (Wave 2B extends this
    the same way: metadata only, never a frame buffer)."""

    x: int
    y: int
    width: int
    height: int
    is_primary: bool


class FocusedControlInfo(BaseModel):
    """Identity metadata of the keyboard-focused UI Automation control --
    deliberately has no `value`/`text` field. A focused control's *content*
    (e.g. a password box's characters) is never carried here; see
    skills/desktop_control.py's read_selected_text/read_clipboard actions
    for the only paths that return control content, and both redact
    IsPassword controls."""

    control_type: str | None = None
    name: str | None = None
    automation_id: str | None = None
    class_name: str | None = None


class ActiveWindowContext(BaseModel):
    """Native active-window context. Wave 2A shipped executable/title/bounds
    only; Wave 2B adds monitor/window-state/focused-control *metadata* --
    still no screenshot/content capture (M2A_SPEC.md requirement 5 continues
    to apply). Selected text and clipboard content are deliberately NOT
    fields on this struct: they are only ever returned by an explicit,
    individually-audited read action (desktop_control's read_selected_text /
    read_clipboard), never attached silently to every action request."""

    executable: str = Field(min_length=1)
    process_id: int | None = None
    window_title: str
    window_bounds: WindowBounds | None = None
    captured_at: datetime | None = None
    window_state: WindowState | None = None
    monitor: MonitorInfo | None = None
    focused_control: FocusedControlInfo | None = None
    source: ContextSource | None = None


class ActionRequest(BaseModel):
    schema_version: str = "1.0.0"
    id: UUID = Field(default_factory=uuid4)
    skill: str = Field(min_length=1)
    action: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    source: ActionSource
    active_context: ActiveWindowContext | None = None
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_contract_dict(self) -> dict:
        """Serializes exactly as contracts/action-request.schema.json
        expects (ISO-8601 strings, plain str uuid, no extra fields)."""
        return self.model_dump(mode="json")


class ActionResult(BaseModel):
    schema_version: str = "1.0.0"
    request_id: UUID
    status: ActionStatus
    risk_level: RiskLevel | None = None
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None

    def to_contract_dict(self) -> dict:
        return self.model_dump(mode="json")


class SkillValidationError(ValueError):
    """Raised by Skill.validate() when arguments are structurally invalid
    for the given action -- distinct from a permission denial, which the
    action runtime decides, not the skill."""


class SkillExecutionError(RuntimeError):
    """Raised by Skill.execute() for an execution-time failure (process
    launch failed, file not found, command timed out, ...). The action
    runtime converts this into an ActionResult with status=FAILED; a Skill
    should not fabricate a SUCCEEDED result to paper over a real failure."""


@runtime_checkable
class Skill(Protocol):
    """Structural interface every Wave 2A skill implements. Concrete skills
    should subclass BaseSkill (below) rather than implementing this Protocol
    from scratch, to get the constructor/name/capabilities plumbing for
    free -- the Protocol exists so the action runtime can type against the
    interface without importing every concrete skill module."""

    name: str
    capabilities: tuple[str, ...]

    def classify_risk(self, action: str, arguments: dict[str, Any]) -> RiskLevel: ...

    async def validate(self, action: str, arguments: dict[str, Any]) -> None: ...

    async def execute(self, request: ActionRequest) -> ActionResult: ...


class BaseSkill(ABC):
    """Convenience base class implementing the Skill interface."""

    name: str
    capabilities: tuple[str, ...] = ()

    @abstractmethod
    def classify_risk(self, action: str, arguments: dict[str, Any]) -> RiskLevel:
        """Must be a pure function of (action, arguments) -- no I/O, no
        randomness. The permission engine may call this ahead of execute()
        to decide whether to prompt for confirmation."""

    @abstractmethod
    async def validate(self, action: str, arguments: dict[str, Any]) -> None:
        """Raise SkillValidationError if arguments are invalid for this
        action. Must not perform the action itself."""

    @abstractmethod
    async def execute(self, request: ActionRequest) -> ActionResult:
        """Perform the action and return a real ActionResult. Never return
        status=SUCCEEDED without having actually performed the action."""

    def _result(
        self,
        request: ActionRequest,
        status: ActionStatus,
        summary: str,
        *,
        risk_level: RiskLevel | None = None,
        data: dict[str, Any] | None = None,
        error: str | None = None,
        started_at: datetime | None = None,
    ) -> ActionResult:
        now = datetime.now(UTC)
        return ActionResult(
            request_id=request.id,
            status=status,
            risk_level=risk_level,
            summary=summary,
            data=data or {},
            error=error,
            started_at=started_at or now,
            completed_at=now if status in TERMINAL_STATUSES else None,
        )
