from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import aiosqlite
import pytest

from actions.registry import SkillRegistry
from actions.runtime import ActionRuntime
from actions.store import ActionStore
from agent_runtime.contracts import (
    AgentDefinition,
    AgentJob,
    AgentMode,
    AgentStatus,
    IntelligenceRequest,
    MemoryContext,
    MemoryItem,
    OrchestratorDecision,
    RuntimeLimits,
    StrategyDefinition,
    StrategySignal,
)
from agent_runtime.errors import AgentConflictError, AgentContractError, DuplicateJobError
from agent_runtime.providers import IntelligenceProviderRegistry
from agent_runtime.quant_boundary import QuantBrainBoundary
from agent_runtime.runtime import AgentRuntime
from agent_runtime.store import AgentStore
from app.action_contracts import (
    ActionRequest,
    ActionResult,
    ActionStatus,
    BaseSkill,
    RiskLevel,
    SkillValidationError,
)


class FakeSkill(BaseSkill):
    name = "test_skill"
    capabilities = ("read", "write")

    def __init__(self, risk: RiskLevel = RiskLevel.READ_ONLY) -> None:
        self.risk = risk
        self.executions = 0

    def classify_risk(self, action: str, arguments: dict[str, Any]) -> RiskLevel:
        return self.risk

    async def validate(self, action: str, arguments: dict[str, Any]) -> None:
        if action not in self.capabilities or not isinstance(arguments, dict):
            raise SkillValidationError("malformed skill call")

    async def execute(self, request: ActionRequest) -> ActionResult:
        self.executions += 1
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            "real action completed",
            data={"executions": self.executions},
        )


class SequenceProvider:
    name = "fake"

    def __init__(self, responses: Iterable[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[IntelligenceRequest] = []

    async def decide(self, request: IntelligenceRequest) -> Any:
        self.calls.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class BlockingProvider:
    name = "blocking"

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def decide(self, request: IntelligenceRequest) -> OrchestratorDecision:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class BlockingSkill(FakeSkill):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def execute(self, request: ActionRequest) -> ActionResult:
        self.executions += 1
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class FakeStrategyProvider:
    name = "quant_brain"

    async def get_definition(self, strategy_id: str) -> StrategyDefinition:
        return StrategyDefinition(strategy_id=strategy_id, name="Breakout", version="7")

    async def get_signals(
        self, definition: StrategyDefinition, *, job_id: Any
    ) -> tuple[StrategySignal, ...]:
        return (
            StrategySignal(
                signal_id="signal-1",
                strategy_id=definition.strategy_id,
                payload={"direction": "LONG"},
                evidence_ids=("qb-run-42",),
            ),
        )


@pytest.fixture
async def runtime_factory(tmp_path):
    connections: list[aiosqlite.Connection] = []

    async def build(
        provider: Any,
        *,
        skill: FakeSkill | None = None,
        strategy: Any = None,
        clock: Any = None,
    ) -> AgentRuntime:
        conn = await aiosqlite.connect(str(tmp_path / f"{uuid4()}.db"))
        conn.row_factory = aiosqlite.Row
        connections.append(conn)
        registry = SkillRegistry([skill] if skill else [])
        action_runtime = ActionRuntime(ActionStore(conn), registry)
        await action_runtime.initialize()
        providers = IntelligenceProviderRegistry([provider])
        runtime = AgentRuntime(
            AgentStore(conn),
            action_runtime,
            providers,
            strategy_boundary=QuantBrainBoundary(strategy),
            clock=clock,
        )
        await runtime.initialize()
        return runtime

    yield build
    for conn in connections:
        await conn.close()


def job(
    mode: AgentMode = AgentMode.ON_DEMAND,
    *,
    limits: RuntimeLimits | None = None,
    strategy_id: str | None = None,
    provider: str = "fake",
    scheduled_for: datetime | None = None,
) -> AgentJob:
    return AgentJob(
        dedupe_key=f"job-{uuid4()}",
        definition=AgentDefinition(
            agent_id="overnight",
            name="Overnight safety agent",
            mode=mode,
            intelligence_provider=provider,
            strategy_id=strategy_id,
            interval_seconds=1 if mode != AgentMode.ON_DEMAND else None,
            limits=limits or RuntimeLimits(),
        ),
        objective="Perform bounded research",
        scheduled_for=scheduled_for,
    )


def action_decision(**extra: Any) -> dict[str, Any]:
    call = {"skill": "test_skill", "action": "read", "arguments": {}}
    call.update(extra)
    return {"kind": "ACTION", "summary": "use a skill", "skill_call": call}


def complete(**output: Any) -> dict[str, Any]:
    return {"kind": "COMPLETE", "summary": "done truthfully", "output": output}


def test_agent_api_exposes_lifecycle_and_duplicate_protection(client):
    provider = SequenceProvider([complete()])
    client.app.state.agent_job_runtime.providers.register(provider)
    payload = job().model_dump(mode="json")

    created = client.post("/api/v1/agent-runtime", json=payload)
    duplicate = client.post("/api/v1/agent-runtime?run_now=false", json=payload)
    audit = client.get(f"/api/v1/agent-runtime/{payload['job_id']}/audit")

    assert created.status_code == 200
    assert created.json()["status"] == "SUCCEEDED"
    assert duplicate.status_code == 409
    assert audit.status_code == 200
    assert audit.json()[-1]["event"] == "JOB_SUCCEEDED"


async def test_provider_can_only_propose_actionruntime_executes(runtime_factory):
    skill = FakeSkill()
    provider = SequenceProvider([action_decision(), complete(result="observed")])
    runtime = await runtime_factory(provider, skill=skill)

    result = await runtime.submit(job())

    assert result.status == AgentStatus.SUCCEEDED
    assert skill.executions == 1
    assert provider.calls[1].last_action_result["status"] == "SUCCEEDED"
    audit = await runtime.store.list_audit(result.job_id)
    assert [row["event"] for row in audit][-1] == "JOB_SUCCEEDED"


async def test_model_cannot_downgrade_risk_or_bypass_confirmation(runtime_factory):
    skill = FakeSkill(RiskLevel.CONFIRM_REQUIRED)
    malformed = SequenceProvider([action_decision(risk_level="READ_ONLY")])
    runtime = await runtime_factory(malformed, skill=skill)
    rejected = await runtime.submit(job())
    assert rejected.status == AgentStatus.FAILED
    assert skill.executions == 0

    provider = SequenceProvider([action_decision()])
    second = await runtime_factory(provider, skill=skill)
    waiting = await second.submit(job())
    assert waiting.status == AgentStatus.WAITING_CONFIRMATION
    action = await second.action_runtime.get_result(waiting.pending_action_id)
    assert action.risk_level == RiskLevel.CONFIRM_REQUIRED
    assert skill.executions == 0


@pytest.mark.parametrize(
    "decision",
    [
        {"kind": "COMPLETE", "summary": "done", "verified": True},
        complete(verification_status="VERIFIED"),
        complete(result="VERIFIED"),
    ],
)
async def test_model_cannot_fabricate_verified(runtime_factory, decision):
    runtime = await runtime_factory(SequenceProvider([decision]))
    result = await runtime.submit(job())
    assert result.status == AgentStatus.FAILED
    assert "no successful outcome was inferred" in result.summary


async def test_continuous_agent_has_bounded_slices_and_lifetime(runtime_factory):
    limits = RuntimeLimits(max_iterations_per_run=2, max_cycles=2)
    waits = [
        {"kind": "WAIT", "summary": "nothing new"},
        {"kind": "WAIT", "summary": "still nothing"},
    ]
    provider = SequenceProvider(waits)
    runtime = await runtime_factory(provider)
    queued = await runtime.submit(job(AgentMode.CONTINUOUS, limits=limits), run_now=False)

    first = await runtime.run(queued.job_id)
    second = await runtime.run(queued.job_id)

    assert first.status == AgentStatus.PAUSED
    assert second.status == AgentStatus.EXHAUSTED
    assert second.cycle == 2
    assert len(provider.calls) == 2
    with pytest.raises(AgentConflictError):
        await runtime.run(queued.job_id)


async def test_on_demand_loop_stops_at_iteration_limit(runtime_factory):
    skill = FakeSkill()
    provider = SequenceProvider([action_decision(), action_decision()])
    limits = RuntimeLimits(max_iterations_per_run=2)
    runtime = await runtime_factory(provider, skill=skill)

    result = await runtime.submit(job(limits=limits))

    assert result.status == AgentStatus.EXHAUSTED
    assert result.iteration == 2
    assert skill.executions == 2


async def test_provider_retries_are_bounded_and_truthful(runtime_factory):
    provider = SequenceProvider([RuntimeError("outage"), RuntimeError("outage")])
    limits = RuntimeLimits(max_provider_retries=1)
    runtime = await runtime_factory(provider)

    result = await runtime.submit(job(limits=limits))

    assert result.status == AgentStatus.FAILED
    assert result.error == "RuntimeError: outage"
    assert len(provider.calls) == 2
    audit = await runtime.store.list_audit(result.job_id)
    assert [entry["event"] for entry in audit].count("PROVIDER_ATTEMPT_FAILED") == 2


async def test_provider_timeout_is_terminal_not_success(runtime_factory):
    provider = BlockingProvider()
    runtime = await runtime_factory(provider)
    limits = RuntimeLimits(provider_timeout_seconds=0.01, max_provider_retries=0)

    result = await runtime.submit(job(limits=limits, provider="blocking"))

    assert result.status == AgentStatus.FAILED
    assert "timeout" in (result.error or "").lower()


async def test_running_job_can_be_cancelled_concurrently(runtime_factory):
    provider = BlockingProvider()
    runtime = await runtime_factory(provider)
    queued = await runtime.submit(job(provider="blocking"), run_now=False)
    task = asyncio.create_task(runtime.run(queued.job_id))
    await asyncio.wait_for(provider.started.wait(), timeout=1)

    cancelling = await runtime.cancel(queued.job_id)
    finished = await asyncio.wait_for(task, timeout=1)

    assert cancelling.status in {AgentStatus.CANCELLING, AgentStatus.CANCELLED}
    assert finished.status == AgentStatus.CANCELLED


async def test_in_flight_action_is_cancelled_through_actionruntime(runtime_factory):
    skill = BlockingSkill()
    runtime = await runtime_factory(SequenceProvider([action_decision()]), skill=skill)
    queued = await runtime.submit(job(), run_now=False)
    task = asyncio.create_task(runtime.run(queued.job_id))
    await asyncio.wait_for(skill.started.wait(), timeout=1)

    await runtime.cancel(queued.job_id)
    finished = await asyncio.wait_for(task, timeout=1)

    assert finished.status == AgentStatus.CANCELLED
    action_audit = await runtime.action_runtime.store.list_recent_audit()
    assert action_audit[0]["event"] == "FAILED"
    assert action_audit[0]["summary"] == "Action execution was cancelled."


async def test_duplicate_id_and_dedupe_key_are_rejected(runtime_factory):
    runtime = await runtime_factory(SequenceProvider([complete()]))
    original = job()
    await runtime.submit(original, run_now=False)

    with pytest.raises(DuplicateJobError):
        await runtime.submit(original, run_now=False)
    with pytest.raises(DuplicateJobError):
        await runtime.submit(
            job().model_copy(update={"dedupe_key": original.dedupe_key}), run_now=False
        )


async def test_secrets_are_rejected_from_memory_and_redacted_from_audit(runtime_factory):
    runtime = await runtime_factory(SequenceProvider([complete()]))
    unsafe = job().model_copy(
        update={
            "memory": MemoryContext(
                items=(
                    MemoryItem(
                        source_id="note",
                        source_type="conversation",
                        text="token=abc",
                    ),
                )
            )
        }
    )
    with pytest.raises(AgentContractError):
        await runtime.submit(unsafe, run_now=False)

    provider = SequenceProvider(
        [{"kind": "COMPLETE", "summary": "password=hunter2", "output": {}}]
    )
    runtime = await runtime_factory(provider)
    result = await runtime.submit(job())
    audit_text = str(await runtime.store.list_audit(result.job_id))
    assert "hunter2" not in audit_text


async def test_no_trade_signal_when_strategy_provider_not_configured(runtime_factory):
    provider = SequenceProvider([complete(trade_signal={"direction": "LONG"})])
    runtime = await runtime_factory(provider)

    result = await runtime.submit(job(strategy_id="breakout-v7"))

    assert provider.calls[0].strategy.availability.value == "NOT_CONFIGURED"
    assert provider.calls[0].strategy.signals == ()
    assert result.status == AgentStatus.FAILED
    assert "StrategyProvider" in (result.error or "")


async def test_quant_brain_context_is_read_only_provider_data(runtime_factory):
    provider = SequenceProvider([complete()])
    runtime = await runtime_factory(provider, strategy=FakeStrategyProvider())

    result = await runtime.submit(job(strategy_id="breakout-v7"))

    assert result.status == AgentStatus.SUCCEEDED
    context = provider.calls[0].strategy
    assert context.availability.value == "AVAILABLE"
    assert context.definition.source == "quant_brain"
    assert context.signals[0].evidence_ids == ("qb-run-42",)


async def test_malformed_skill_call_is_rejected_before_action(runtime_factory):
    provider = SequenceProvider(
        [
            {
                "kind": "ACTION",
                "summary": "bad",
                "skill_call": {"skill": "test_skill", "action": "read", "arguments": []},
            }
        ]
    )
    skill = FakeSkill()
    runtime = await runtime_factory(provider, skill=skill)

    result = await runtime.submit(job())

    assert result.status == AgentStatus.FAILED
    assert skill.executions == 0


async def test_due_scheduled_job_runs_only_when_due(runtime_factory):
    current = datetime(2026, 8, 18, tzinfo=UTC)
    provider = SequenceProvider([complete()])
    runtime = await runtime_factory(provider, clock=lambda: current)
    scheduled = await runtime.submit(
        job(AgentMode.SCHEDULED, scheduled_for=current + timedelta(minutes=1)),
        run_now=False,
    )

    assert await runtime.run_due() == []
    current += timedelta(minutes=2)
    results = await runtime.run_due()
    assert results[0].job_id == scheduled.job_id
    assert results[0].status == AgentStatus.SUCCEEDED


async def test_interrupted_run_requires_explicit_recovery(runtime_factory):
    provider = SequenceProvider([complete()])
    runtime = await runtime_factory(provider)
    queued = await runtime.submit(job(), run_now=False)
    await runtime.store.claim(queued.job_id, datetime.now(UTC))

    recovered_ids = await runtime.initialize()

    assert recovered_ids == [queued.job_id]
    assert (await runtime.store.get_run(queued.job_id)).status == AgentStatus.RECOVERY_REQUIRED
    with pytest.raises(AgentConflictError):
        await runtime.run(queued.job_id)
    recovered = await runtime.recover(queued.job_id)
    assert recovered.status == AgentStatus.PAUSED
    assert (await runtime.run(queued.job_id)).status == AgentStatus.SUCCEEDED
