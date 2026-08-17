from __future__ import annotations

from typing import Any

import pytest

from app.action_contracts import (
    ActionRequest,
    ActionResult,
    ActionSource,
    ActionStatus,
    ActiveWindowContext,
    BaseSkill,
    RiskLevel,
    Skill,
    SkillValidationError,
    WindowBounds,
)
from app.contracts import ContractValidationError, validate_action_request, validate_action_result


def test_minimal_action_request_passes_contract():
    request = ActionRequest(
        skill="windows_app",
        action="launch",
        arguments={"target": "notepad.exe"},
        source=ActionSource.hud,
    )
    validate_action_request(request.to_contract_dict())


def test_action_request_with_active_context_passes_contract():
    request = ActionRequest(
        skill="terminal",
        action="run_command",
        arguments={"command": "dir"},
        source=ActionSource.deterministic,
        active_context=ActiveWindowContext(
            executable="explorer.exe",
            process_id=1234,
            window_title="File Explorer",
            window_bounds=WindowBounds(x=0, y=0, width=1920, height=1080),
        ),
    )
    validate_action_request(request.to_contract_dict())


def test_action_request_unknown_field_is_rejected():
    request = ActionRequest(
        skill="browser", action="open_url", arguments={}, source=ActionSource.hud
    )
    payload = request.to_contract_dict()
    payload["confidence"] = 0.9
    with pytest.raises(ContractValidationError):
        validate_action_request(payload)


def test_action_result_pending_and_terminal_pass_contract():
    request_id = ActionRequest(
        skill="filesystem", action="list", arguments={}, source=ActionSource.hud
    ).id

    pending = ActionResult(
        request_id=request_id,
        status=ActionStatus.CONFIRMATION_REQUIRED,
        risk_level=RiskLevel.CONFIRM_REQUIRED,
        summary="Deleting 3 files requires confirmation.",
    )
    validate_action_result(pending.to_contract_dict())
    assert pending.to_contract_dict()["completed_at"] is None

    done = ActionResult(
        request_id=request_id,
        status=ActionStatus.SUCCEEDED,
        risk_level=RiskLevel.LOW_RISK,
        summary="Listed 3 files.",
        data={"files": ["a.txt", "b.txt", "c.txt"]},
        completed_at=pending.started_at,
    )
    validate_action_result(done.to_contract_dict())


def test_action_result_unknown_field_is_rejected():
    result = ActionResult(
        request_id=ActionRequest(
            skill="browser", action="open_url", arguments={}, source=ActionSource.hud
        ).id,
        status=ActionStatus.SUCCEEDED,
        summary="ok",
    )
    payload = result.to_contract_dict()
    payload["ai_confidence"] = 0.99
    with pytest.raises(ContractValidationError):
        validate_action_result(payload)


class _EchoSkill(BaseSkill):
    name = "echo"
    capabilities = ("echo",)

    def classify_risk(self, action: str, arguments: dict[str, Any]) -> RiskLevel:
        return RiskLevel.READ_ONLY

    async def validate(self, action: str, arguments: dict[str, Any]) -> None:
        if "text" not in arguments:
            raise SkillValidationError("missing 'text'")

    async def execute(self, request: ActionRequest) -> ActionResult:
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            f"echoed {request.arguments['text']!r}",
            risk_level=RiskLevel.READ_ONLY,
            data={"text": request.arguments["text"]},
        )


def test_base_skill_satisfies_skill_protocol():
    skill = _EchoSkill()
    assert isinstance(skill, Skill)


async def test_base_skill_execute_produces_valid_result():
    skill = _EchoSkill()
    request = ActionRequest(
        skill="echo", action="echo", arguments={"text": "hi"}, source=ActionSource.hud
    )
    await skill.validate(request.action, request.arguments)
    result = await skill.execute(request)
    assert result.status == ActionStatus.SUCCEEDED
    assert result.completed_at is not None
    validate_action_result(result.to_contract_dict())


async def test_base_skill_validate_rejects_missing_argument():
    skill = _EchoSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("echo", {})
