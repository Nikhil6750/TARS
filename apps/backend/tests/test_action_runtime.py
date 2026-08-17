from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from actions.registry import SkillRegistryError
from app.action_contracts import (
    ActionRequest,
    ActionResult,
    ActionStatus,
    BaseSkill,
    RiskLevel,
    SkillExecutionError,
    SkillValidationError,
)


class FakeSkill(BaseSkill):
    capabilities = ("act",)

    def __init__(
        self,
        name: str,
        risk: RiskLevel,
        *,
        fail: bool = False,
        invalid_result: bool = False,
    ) -> None:
        self.name = name
        if name == "terminal":
            self.capabilities = ("run_command",)
        elif name == "browser":
            self.capabilities = ("open_url",)
        elif name == "windows_app":
            self.capabilities = ("focus", "launch")
        self.risk = risk
        self.fail = fail
        self.invalid_result = invalid_result
        self.executions = 0

    def classify_risk(self, action: str, arguments: dict[str, Any]) -> RiskLevel:
        return self.risk

    async def validate(self, action: str, arguments: dict[str, Any]) -> None:
        if action not in self.capabilities:
            raise SkillValidationError("unsupported action")
        if arguments.get("malformed"):
            raise SkillValidationError("malformed arguments")

    async def execute(self, request: ActionRequest) -> ActionResult:
        self.executions += 1
        if self.fail:
            raise SkillExecutionError("real execution failure")
        if self.invalid_result:
            return self._result(
                request.model_copy(update={"id": uuid4()}),
                ActionStatus.SUCCEEDED,
                "wrong request",
            )
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            "Action actually completed.",
            data={"executions": self.executions},
        )


def register(client, skill: FakeSkill) -> FakeSkill:
    client.app.state.action_registry.register(skill)
    return skill


def payload(
    skill: str = "reader",
    action: str = "act",
    arguments: dict[str, Any] | None = None,
    *,
    request_id: str | None = None,
    requested_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "id": request_id or str(uuid4()),
        "skill": skill,
        "action": action,
        "arguments": arguments or {},
        "source": "api",
        "active_context": None,
        "requested_at": (requested_at or datetime.now(UTC)).isoformat(),
    }


def test_read_only_action_executes_and_has_audit(client):
    skill = register(client, FakeSkill("reader", RiskLevel.READ_ONLY))
    body = payload()

    response = client.post("/api/v1/actions", json=body)

    assert response.status_code == 200
    assert response.json()["status"] == "SUCCEEDED"
    assert response.json()["risk_level"] == "READ_ONLY"
    assert skill.executions == 1
    audit = client.get(f"/api/v1/actions/{body['id']}/audit").json()
    assert [entry["event"] for entry in audit] == ["REQUESTED", "RUNNING", "SUCCEEDED"]


def test_duplicate_request_id_is_rejected_without_second_execution(client):
    skill = register(client, FakeSkill("reader", RiskLevel.READ_ONLY))
    body = payload()
    assert client.post("/api/v1/actions", json=body).status_code == 200

    duplicate = client.post("/api/v1/actions", json=body)

    assert duplicate.status_code == 409
    assert skill.executions == 1


@pytest.mark.parametrize(
    "mutation",
    [
        lambda body: body.update({"confirmed": True}),
        lambda body: body.update({"schema_version": "9.9.9"}),
        lambda body: body.update({"arguments": []}),
        lambda body: body.pop("skill"),
    ],
)
def test_malformed_contract_payloads_never_execute(client, mutation):
    skill = register(client, FakeSkill("reader", RiskLevel.READ_ONLY))
    body = payload()
    mutation(body)

    response = client.post("/api/v1/actions", json=body)

    assert response.status_code == 422
    assert skill.executions == 0


def test_skill_argument_validation_denies_without_execution(client):
    skill = register(client, FakeSkill("reader", RiskLevel.READ_ONLY))

    response = client.post(
        "/api/v1/actions", json=payload(arguments={"malformed": True})
    )

    assert response.json()["status"] == "DENIED"
    assert response.json()["risk_level"] == "BLOCKED"
    assert skill.executions == 0


@pytest.mark.parametrize(
    "requested_at",
    [datetime.now(UTC) - timedelta(minutes=6), datetime.now(UTC) + timedelta(minutes=1)],
)
def test_expired_or_future_action_is_denied_before_skill_routing(client, requested_at):
    skill = register(client, FakeSkill("reader", RiskLevel.READ_ONLY))

    response = client.post(
        "/api/v1/actions", json=payload(requested_at=requested_at)
    )

    assert response.json()["status"] == "DENIED"
    assert "timestamp" in response.json()["summary"]
    assert skill.executions == 0


def test_unknown_skill_is_denied_and_audited(client):
    body = payload(skill="not_registered")

    response = client.post("/api/v1/actions", json=body)

    assert response.json()["status"] == "DENIED"
    audit = client.get(f"/api/v1/actions/{body['id']}/audit").json()
    assert audit[-1]["event"] == "UNKNOWN_SKILL"


def test_declared_read_only_cannot_bypass_known_skill_policy(client):
    skill = register(client, FakeSkill("browser", RiskLevel.READ_ONLY))
    # The browser skill validates only open_url, while the deterministic policy
    # blocks every other browser verb before execute regardless of self-report.
    skill.capabilities = ("delete",)

    response = client.post(
        "/api/v1/actions", json=payload("browser", "delete", {"target": "history"})
    )

    assert response.json()["status"] == "BLOCKED"
    assert skill.executions == 0


def test_blocked_terminal_command_cannot_reach_confirmation_or_execution(client):
    skill = register(client, FakeSkill("terminal", RiskLevel.READ_ONLY))

    response = client.post(
        "/api/v1/actions",
        json=payload(
            "terminal",
            "run_command",
            {"command": "Remove-Item C:\\Windows -Recurse -Force", "confirmed": True},
        ),
    )

    assert response.json()["status"] == "BLOCKED"
    assert "confirmation_token" not in response.json()["data"]
    assert skill.executions == 0


def test_state_changing_terminal_command_requires_exact_one_time_confirmation(client):
    skill = register(client, FakeSkill("terminal", RiskLevel.READ_ONLY))
    body = payload(
        "terminal", "run_command", {"command": "New-Item report.txt", "confirmed": True}
    )

    pending = client.post("/api/v1/actions", json=body)

    assert pending.json()["status"] == "CONFIRMATION_REQUIRED"
    assert pending.json()["data"]["arguments"]["command"] == "New-Item report.txt"
    assert skill.executions == 0
    token = pending.json()["data"]["confirmation_token"]
    stored = client.get(f"/api/v1/actions/{body['id']}").json()
    assert "confirmation_token" not in stored["data"]

    confirmed = client.post(
        f"/api/v1/actions/{body['id']}/confirm",
        json={"confirmation_token": token, "approved": True},
    )
    replay = client.post(
        f"/api/v1/actions/{body['id']}/confirm",
        json={"confirmation_token": token, "approved": True},
    )

    assert confirmed.json()["status"] == "SUCCEEDED"
    assert replay.status_code == 409
    assert skill.executions == 1


def test_wrong_confirmation_token_does_not_consume_valid_token(client):
    skill = register(client, FakeSkill("terminal", RiskLevel.CONFIRM_REQUIRED))
    body = payload("terminal", "run_command", {"command": "Set-Content x.txt hello"})
    pending = client.post("/api/v1/actions", json=body).json()

    wrong = client.post(
        f"/api/v1/actions/{body['id']}/confirm",
        json={"confirmation_token": "wrong", "approved": True},
    )
    correct = client.post(
        f"/api/v1/actions/{body['id']}/confirm",
        json={"confirmation_token": pending["data"]["confirmation_token"], "approved": True},
    )

    assert wrong.status_code == 403
    assert correct.json()["status"] == "SUCCEEDED"
    assert skill.executions == 1


def test_declined_confirmation_is_terminal_and_does_not_execute(client):
    skill = register(client, FakeSkill("terminal", RiskLevel.CONFIRM_REQUIRED))
    body = payload("terminal", "run_command", {"command": "Set-Content x.txt hello"})
    pending = client.post("/api/v1/actions", json=body).json()

    denied = client.post(
        f"/api/v1/actions/{body['id']}/confirm",
        json={"confirmation_token": pending["data"]["confirmation_token"], "approved": False},
    )

    assert denied.json()["status"] == "DENIED"
    assert skill.executions == 0


def test_expired_confirmation_is_denied_without_execution(client):
    skill = register(client, FakeSkill("terminal", RiskLevel.CONFIRM_REQUIRED))
    runtime = client.app.state.action_runtime
    current = datetime.now(UTC)
    runtime._clock = lambda: current
    body = payload(
        "terminal", "run_command", {"command": "Set-Content x.txt hello"}, requested_at=current
    )
    pending = client.post("/api/v1/actions", json=body).json()
    runtime._clock = lambda: current + timedelta(minutes=3)

    expired = client.post(
        f"/api/v1/actions/{body['id']}/confirm",
        json={"confirmation_token": pending["data"]["confirmation_token"], "approved": True},
    )

    assert expired.json()["status"] == "DENIED"
    assert expired.json()["error"] == "Confirmation expired"
    assert skill.executions == 0


def test_execution_failure_is_converted_to_failed_result(client):
    skill = register(client, FakeSkill("reader", RiskLevel.READ_ONLY, fail=True))

    response = client.post("/api/v1/actions", json=payload())

    assert response.json()["status"] == "FAILED"
    assert response.json()["error"] == "real execution failure"
    assert skill.executions == 1


def test_malformed_skill_result_is_failed_not_fabricated_success(client):
    register(client, FakeSkill("reader", RiskLevel.READ_ONLY, invalid_result=True))

    response = client.post("/api/v1/actions", json=payload())

    assert response.json()["status"] == "FAILED"
    assert "request_id mismatch" in response.json()["error"]


def test_active_context_is_attached_to_request_and_audit(client):
    register(client, FakeSkill("reader", RiskLevel.READ_ONLY))
    body = payload()
    body["active_context"] = {
        "executable": "code.exe",
        "process_id": 42,
        "window_title": "TARS — Visual Studio Code",
        "window_bounds": {"x": 1, "y": 2, "width": 1000, "height": 800},
        "captured_at": datetime.now(UTC).isoformat(),
    }

    assert client.post("/api/v1/actions", json=body).json()["status"] == "SUCCEEDED"
    audit = client.get(f"/api/v1/actions/{body['id']}/audit").json()

    assert audit[0]["details"]["active_context"]["executable"] == "code.exe"
    assert "screenshot" not in audit[0]["details"]["active_context"]


def test_assistant_proposal_cannot_supply_permission_identity_or_source(client):
    skill = register(client, FakeSkill("reader", RiskLevel.READ_ONLY))
    proposal = {
        "skill": "reader",
        "action": "act",
        "arguments": {},
        "risk_level": "READ_ONLY",
        "confirmed": True,
        "id": str(uuid4()),
    }

    response = client.post(
        "/api/v1/actions/assistant",
        json={"proposal": proposal, "source": "voice_ptt"},
    )

    assert response.status_code == 422
    assert skill.executions == 0


def test_assistant_context_cannot_smuggle_screenshot_or_window_content(client):
    skill = register(client, FakeSkill("reader", RiskLevel.READ_ONLY))

    response = client.post(
        "/api/v1/actions/assistant",
        json={
            "proposal": {"skill": "reader", "action": "act", "arguments": {}},
            "source": "hud",
            "active_context": {
                "executable": "notepad.exe",
                "window_title": "Notes",
                "screenshot": "base64-data-must-not-be-accepted",
            },
        },
    )

    assert response.status_code == 422
    assert skill.executions == 0


def test_valid_assistant_proposal_uses_trusted_source_and_context(client):
    register(client, FakeSkill("reader", RiskLevel.READ_ONLY))

    response = client.post(
        "/api/v1/actions/assistant",
        json={
            "proposal": {"skill": "reader", "action": "act", "arguments": {}},
            "source": "voice_ptt",
            "active_context": {"executable": "notepad.exe", "window_title": "Notes"},
        },
    )

    assert response.json()["status"] == "SUCCEEDED"
    request_id = response.json()["request_id"]
    audit = client.get(f"/api/v1/actions/{request_id}/audit").json()
    assert audit[0]["details"]["source"] == "voice_ptt"
    assert audit[0]["details"]["active_context"]["window_title"] == "Notes"


def test_deterministic_fixed_phrase_routes_without_assistant_payload(client):
    skill = register(client, FakeSkill("windows_app", RiskLevel.READ_ONLY))

    response = client.post(
        "/api/v1/actions/resolve",
        json={"text": "focus Notepad"},
    )

    assert response.json()["status"] == "SUCCEEDED"
    assert response.json()["risk_level"] == "LOW_RISK"
    assert skill.executions == 1


def test_registry_rejects_duplicate_skill_names(client):
    register(client, FakeSkill("reader", RiskLevel.READ_ONLY))
    with pytest.raises(SkillRegistryError):
        register(client, FakeSkill("reader", RiskLevel.READ_ONLY))


def test_action_result_is_broadcast_on_dedicated_stream(client):
    register(client, FakeSkill("reader", RiskLevel.READ_ONLY))
    with client.websocket_connect("/ws/actions") as websocket:
        assert websocket.receive_json() == {"type": "action_stream_ready"}
        response = client.post("/api/v1/actions", json=payload())
        assert response.json()["status"] == "SUCCEEDED"
        running = websocket.receive_json()
        done = websocket.receive_json()

    assert running["type"] == "action_result"
    assert running["result"]["status"] == "RUNNING"
    assert done["result"]["status"] == "SUCCEEDED"
