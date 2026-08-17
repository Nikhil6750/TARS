from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from app.action_contracts import (
    ActionRequest,
    ActionStatus,
    BaseSkill,
    RiskLevel,
    SkillExecutionError,
    SkillValidationError,
)


class PlanSkill(BaseSkill):
    def __init__(
        self,
        name: str = "control",
        capabilities: tuple[str, ...] = ("inspect", "write", "delete"),
        *,
        failures: int = 0,
        delay: float = 0,
    ) -> None:
        self.name = name
        self.capabilities = capabilities
        self.failures = failures
        self.delay = delay
        self.executions: list[dict[str, Any]] = []

    def classify_risk(self, action: str, arguments: dict[str, Any]) -> RiskLevel:
        return RiskLevel.READ_ONLY

    async def validate(self, action: str, arguments: dict[str, Any]) -> None:
        if action not in self.capabilities or arguments.get("malformed"):
            raise SkillValidationError("invalid action")

    async def execute(self, request: ActionRequest):
        self.executions.append(request.arguments)
        if self.delay:
            await asyncio.sleep(self.delay)
        if len(self.executions) <= self.failures:
            raise SkillExecutionError("recoverable failure")
        return self._result(request, ActionStatus.SUCCEEDED, "performed")


def register(client, skill: PlanSkill) -> PlanSkill:
    client.app.state.action_registry.register(skill, replace=True)
    return skill


def step(
    action: str = "inspect",
    *,
    step_id: str | None = None,
    expected: dict[str, Any] | None = None,
    risk: str | None = None,
    dependencies: list[str] | None = None,
    recovery: dict[str, Any] | None = None,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "step_id": step_id or str(uuid4()),
        "skill": "control",
        "action": action,
        "arguments": arguments or {},
        "expected_result": expected or {"ready": True},
        "status": "PENDING",
        "dependencies": dependencies or [],
    }
    if risk:
        body["risk_level"] = risk
    if recovery:
        body["recovery"] = recovery
    return body


def plan(steps: list[dict[str, Any]], **context: Any) -> dict[str, Any]:
    return {
        "plan_id": str(uuid4()),
        "goal": "Complete a bounded control task",
        "context": context,
        "steps": steps,
        "status": "PLANNED",
        "created_at": datetime.now(UTC).isoformat(),
        "provenance": "API",
    }


def observation(execution: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return {
        "observation_id": str(uuid4()),
        "plan_id": execution["plan"]["plan_id"],
        "step_id": execution["current_step_id"],
        "request_id": execution["active_request_id"],
        "source": "WINDOWS_UI_AUTOMATION",
        "state": state,
        "observed_at": datetime.now(UTC).isoformat(),
    }


def test_plan_executes_one_action_then_requires_backend_verification(client):
    skill = register(client, PlanSkill())
    body = plan([step()])

    pending = client.post("/api/v1/action-plans", json=body).json()

    assert pending["plan"]["status"] == "WAITING_OBSERVATION"
    assert skill.executions == [{}]
    done = client.post(
        f"/api/v1/action-plans/{body['plan_id']}/observations",
        json=observation(pending, {"ready": True, "extra": "allowed"}),
    ).json()
    assert done["plan"]["status"] == "COMPLETED"
    assert done["verification"]["status"] == "VERIFIED"


def test_model_risk_downgrade_cannot_bypass_confirmation(client):
    skill = register(client, PlanSkill())
    body = plan([step("write", risk="READ_ONLY")])

    pending = client.post("/api/v1/action-plans", json=body).json()

    assert pending["plan"]["status"] == "WAITING_CONFIRMATION"
    assert pending["plan"]["steps"][0]["risk_level"] == "CONFIRM_REQUIRED"
    assert pending["pending_operation"]["action"] == "write"
    assert pending["pending_operation"]["confirmation_token"]
    assert skill.executions == []


def test_confirmation_cannot_be_bypassed_or_replayed(client):
    skill = register(client, PlanSkill())
    body = plan([step("write")])
    pending = client.post("/api/v1/action-plans", json=body).json()
    path = f"/api/v1/action-plans/{body['plan_id']}/confirm"
    confirm = {
        "step_id": pending["current_step_id"],
        "request_id": pending["active_request_id"],
        "confirmation_token": pending["pending_operation"]["confirmation_token"],
        "approved": True,
    }

    assert client.post(path, json={**confirm, "confirmation_token": "forged"}).status_code == 403
    approved = client.post(path, json=confirm)
    replay = client.post(path, json=confirm)

    assert approved.json()["plan"]["status"] == "WAITING_OBSERVATION"
    assert replay.status_code == 409
    assert len(skill.executions) == 1


def test_blocked_later_step_stops_multi_step_plan(client):
    skill = register(client, PlanSkill())
    first = step()
    second = step("delete", dependencies=[first["step_id"]])
    body = plan([first, second])
    pending = client.post("/api/v1/action-plans", json=body).json()

    blocked = client.post(
        f"/api/v1/action-plans/{body['plan_id']}/observations",
        json=observation(pending, {"ready": True}),
    ).json()

    assert blocked["plan"]["status"] == "BLOCKED"
    assert blocked["plan"]["steps"][1]["status"] == "BLOCKED"
    assert len(skill.executions) == 1


def test_duplicate_plan_and_step_ids_are_rejected(client):
    register(client, PlanSkill())
    duplicate = str(uuid4())
    malformed = plan([step(step_id=duplicate), step(step_id=duplicate)])
    assert client.post("/api/v1/action-plans", json=malformed).status_code == 422

    valid = plan([step()])
    assert client.post("/api/v1/action-plans", json=valid).status_code == 200
    assert client.post("/api/v1/action-plans", json=valid).status_code == 409


def test_step_bound_prevents_infinite_plan(client):
    register(client, PlanSkill())
    body = plan([step() for _ in range(13)])

    response = client.post("/api/v1/action-plans", json=body)

    assert response.status_code == 422
    assert "exceeds 12 steps" in response.json()["detail"]


def test_retry_is_bounded_and_each_attempt_is_a_new_action_request(client):
    skill = register(client, PlanSkill(failures=99))
    body = plan([step(recovery={"allow_retry": True})])

    failed = client.post("/api/v1/action-plans", json=body).json()
    audit = client.get(f"/api/v1/action-plans/{body['plan_id']}/audit").json()

    assert failed["plan"]["status"] == "FAILED"
    requests = [entry["request_id"] for entry in audit if entry["event"] == "ACTION_PROPOSED"]
    assert len(requests) == 3
    assert len(set(requests)) == 3
    assert len(skill.executions) == 3
    assert any(entry["event"] == "RETRY_EXHAUSTED" or entry["event"] == "PLAN_FAILED" for entry in audit)


def test_forged_verification_and_malformed_observation_are_rejected(client):
    register(client, PlanSkill())
    body = plan([step()])
    pending = client.post("/api/v1/action-plans", json=body).json()
    forged = observation(pending, {"ready": False, "verification_status": "VERIFIED"})

    assert client.post(
        f"/api/v1/action-plans/{body['plan_id']}/observations", json=forged
    ).status_code == 422
    malformed = observation(pending, {"ready": True})
    malformed["state"] = "not-structured"
    assert client.post(
        f"/api/v1/action-plans/{body['plan_id']}/observations", json=malformed
    ).status_code == 422


def test_unknown_verification_never_completes(client):
    register(client, PlanSkill())
    body = plan([step(expected={"ready": True})])
    pending = client.post("/api/v1/action-plans", json=body).json()

    failed = client.post(
        f"/api/v1/action-plans/{body['plan_id']}/observations",
        json=observation(pending, {"other": True}),
    ).json()

    assert failed["verification"]["status"] == "UNKNOWN"
    assert failed["plan"]["status"] == "FAILED"


def test_sensitive_context_blocks_next_state_change_and_redacts_audit(client):
    skill = register(client, PlanSkill())
    first = step()
    second = step("write", dependencies=[first["step_id"]])
    body = plan([first, second])
    pending = client.post("/api/v1/action-plans", json=body).json()
    obs = observation(
        pending,
        {"ready": True, "control_type": "password", "password": "do-not-store"},
    )

    blocked = client.post(
        f"/api/v1/action-plans/{body['plan_id']}/observations", json=obs
    ).json()
    audit = client.get(f"/api/v1/action-plans/{body['plan_id']}/audit").json()

    assert blocked["plan"]["status"] == "BLOCKED"
    serialized = str(audit)
    assert "do-not-store" not in serialized
    assert "PASSWORD_INPUT" in serialized
    assert len(skill.executions) == 1


def test_plan_cancellation_is_terminal_and_prevents_continuation(client):
    skill = register(client, PlanSkill())
    body = plan([step()])
    pending = client.post("/api/v1/action-plans", json=body).json()

    cancelled = client.post(f"/api/v1/action-plans/{body['plan_id']}/cancel").json()
    after = client.post(
        f"/api/v1/action-plans/{body['plan_id']}/observations",
        json=observation(pending, {"ready": True}),
    )

    assert cancelled["plan"]["status"] == "CANCELLED"
    assert after.status_code == 409
    assert len(skill.executions) == 1


def test_cancelling_plan_invalidates_pending_action_confirmation(client):
    skill = register(client, PlanSkill())
    body = plan([step("write")])
    pending = client.post("/api/v1/action-plans", json=body).json()
    token = pending["pending_operation"]["confirmation_token"]

    cancelled = client.post(f"/api/v1/action-plans/{body['plan_id']}/cancel")
    bypass = client.post(
        f"/api/v1/actions/{pending['active_request_id']}/confirm",
        json={"confirmation_token": token, "approved": True},
    )

    assert cancelled.json()["plan"]["status"] == "CANCELLED"
    assert bypass.status_code == 409
    assert skill.executions == []


def test_arbitrary_code_and_assistant_control_fields_are_rejected(client):
    register(client, PlanSkill())
    coded = plan([step(arguments={"script": "click_everything()"})])
    assert client.post("/api/v1/action-plans", json=coded).status_code == 422

    response = client.post(
        "/api/v1/action-plans/assistant",
        json={
            "proposal": {
                "goal": "bypass",
                "status": "COMPLETED",
                "steps": [step()],
            }
        },
    )
    assert response.status_code == 422


def test_assistant_plan_is_data_only_and_still_uses_permission_engine(client):
    skill = register(client, PlanSkill())
    proposed_step = step("write", risk="READ_ONLY")
    proposed_step.pop("status")

    response = client.post(
        "/api/v1/action-plans/assistant",
        json={
            "proposal": {
                "goal": "Make a structured change",
                "context": {},
                "steps": [proposed_step],
            }
        },
    ).json()

    assert response["plan"]["provenance"] == "ASSISTANT"
    assert response["plan"]["status"] == "WAITING_CONFIRMATION"
    assert response["plan"]["steps"][0]["risk_level"] == "CONFIRM_REQUIRED"
    assert skill.executions == []


def test_reobservation_and_observation_replay_are_bounded(client):
    register(client, PlanSkill())
    body = plan([step(recovery={"allow_reobserve": True})])
    pending = client.post("/api/v1/action-plans", json=body).json()
    unknown = observation(pending, {"other": True})

    still_waiting = client.post(
        f"/api/v1/action-plans/{body['plan_id']}/observations", json=unknown
    )
    replay = client.post(
        f"/api/v1/action-plans/{body['plan_id']}/observations", json=unknown
    )

    assert still_waiting.json()["plan"]["status"] == "WAITING_OBSERVATION"
    assert still_waiting.json()["verification"]["status"] == "UNKNOWN"
    assert replay.status_code == 409


def test_explicit_alternate_target_is_validated_and_used_once(client):
    skill = register(client, PlanSkill(failures=1))
    body = plan(
        [
            step(
                arguments={"target": "primary"},
                recovery={
                    "allow_retry": True,
                    "alternate_arguments": [{"target": "fallback"}],
                },
            )
        ]
    )

    pending = client.post("/api/v1/action-plans", json=body).json()

    assert pending["plan"]["status"] == "WAITING_OBSERVATION"
    assert skill.executions == [{"target": "primary"}, {"target": "fallback"}]


def test_plan_timeout_is_terminal_before_execution(client):
    skill = register(client, PlanSkill(delay=0.2))
    client.app.state.plan_runtime.plan_timeout = timedelta(milliseconds=50)
    body = plan([step()])

    timed_out = client.post("/api/v1/action-plans", json=body).json()

    assert timed_out["plan"]["status"] == "TIMED_OUT"
    assert len(skill.executions) <= 1


def test_secure_desktop_context_blocks_state_change(client):
    skill = register(client, PlanSkill())
    body = plan([step("write")], secure_desktop=True)

    blocked = client.post("/api/v1/action-plans", json=body).json()

    assert blocked["plan"]["status"] == "BLOCKED"
    assert "safety policy" in blocked["error"] or "sensitive context" in blocked["error"]
    assert skill.executions == []


def test_action_and_plan_audits_redact_secret_shaped_values(client):
    register(client, PlanSkill())
    action = {
        "schema_version": "1.0.0",
        "id": str(uuid4()),
        "skill": "control",
        "action": "inspect",
        "arguments": {"api_token": "raw-action-secret"},
        "source": "api",
        "active_context": None,
        "requested_at": datetime.now(UTC).isoformat(),
    }
    assert client.post("/api/v1/actions", json=action).status_code == 200
    action_audit = client.get(f"/api/v1/actions/{action['id']}/audit").json()

    body = plan([step()])
    body["goal"] = "Use token=raw-plan-secret safely"
    assert client.post("/api/v1/action-plans", json=body).status_code == 200
    plan_audit = client.get(f"/api/v1/action-plans/{body['plan_id']}/audit").json()

    serialized = str(action_audit) + str(plan_audit)
    assert "raw-action-secret" not in serialized
    assert "raw-plan-secret" not in serialized
    assert "[REDACTED]" in serialized
