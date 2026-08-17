from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from actions.requests import ActionRequestFactory
from actions.runtime import ActionRuntime
from agents.contracts import (
    AgentJob,
    AgentMode,
    AgentRun,
    AgentStatus,
    DecisionKind,
    IntelligenceRequest,
    OrchestratorDecision,
    SkillDiscoveryProvider,
    StrategyAvailability,
    StrategyContext,
)
from agents.errors import AgentConflictError, AgentContractError
from agents.providers import IntelligenceProviderRegistry
from agents.quant_boundary import QuantBrainBoundary
from agents.safety import assert_secret_free
from agents.skill_discovery import ActionRuntimeSkillDiscovery
from agents.store import AgentStore
from app.action_contracts import ActionRequest, ActionResult, ActionSource, ActionStatus


class AgentRuntime:
    """Durable bounded orchestrator. Providers propose data; they never execute."""

    def __init__(
        self,
        store: AgentStore,
        action_runtime: ActionRuntime,
        providers: IntelligenceProviderRegistry,
        *,
        strategy_boundary: QuantBrainBoundary | None = None,
        skill_discovery: SkillDiscoveryProvider | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.action_runtime = action_runtime
        self.providers = providers
        self.strategy_boundary = strategy_boundary or QuantBrainBoundary()
        self.skill_discovery = skill_discovery or ActionRuntimeSkillDiscovery(action_runtime)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._cancel_events: dict[UUID, asyncio.Event] = {}

    async def initialize(self) -> list[UUID]:
        await self.store.initialize()
        return await self.store.mark_interrupted(self._now())

    async def submit(self, job: AgentJob, *, run_now: bool = True) -> AgentRun:
        job = AgentJob.model_validate(job.model_dump(mode="python"))
        assert_secret_free(job.memory.model_dump(mode="json"), label="memory context")
        assert_secret_free(job.objective, label="agent objective")
        initial = (
            AgentStatus.SCHEDULED
            if job.definition.mode == AgentMode.SCHEDULED
            else AgentStatus.READY
        )
        run = await self.store.insert(job, initial, self._now())
        if run_now and job.definition.mode == AgentMode.ON_DEMAND:
            return await self.run(job.job_id)
        return run

    async def run(self, job_id: UUID) -> AgentRun:
        async with self._lock(job_id):
            job = await self.store.get_job(job_id)
            record = await self.store.require_record(job_id)
            status = AgentStatus(record["status"])
            if status in {
                AgentStatus.CANCELLED,
                AgentStatus.SUCCEEDED,
                AgentStatus.FAILED,
                AgentStatus.TIMED_OUT,
                AgentStatus.EXHAUSTED,
            }:
                raise AgentConflictError(f"agent job is terminal: {status.value}")
            if status == AgentStatus.RECOVERY_REQUIRED:
                raise AgentConflictError("agent job requires explicit recovery")

            continuation = False
            pending_id = record["pending_action_id"]
            if pending_id:
                action_result = await self.action_runtime.get_result(UUID(pending_id))
                if action_result.status == ActionStatus.CONFIRMATION_REQUIRED:
                    return await self.store.get_run(job_id)
                if action_result.status not in {
                    ActionStatus.SUCCEEDED,
                    ActionStatus.FAILED,
                    ActionStatus.DENIED,
                    ActionStatus.BLOCKED,
                }:
                    raise AgentConflictError("pending action is not terminal")
                await self.store.update(
                    job_id,
                    self._now(),
                    status=AgentStatus.PAUSED,
                    summary="Confirmed action completed; agent can continue.",
                    last_action=action_result.to_contract_dict(),
                    clear_pending_action=True,
                )
                continuation = True
                record = await self.store.require_record(job_id)

            if (
                job.definition.mode == AgentMode.CONTINUOUS
                and int(record["cycle"]) >= job.definition.limits.max_cycles
                and not continuation
            ):
                return await self._finish(
                    job,
                    AgentStatus.EXHAUSTED,
                    "Continuous agent reached its configured lifetime cycle limit.",
                    event="CYCLE_LIMIT_REACHED",
                )

            record = await self.store.claim(
                job_id, self._now(), increment_cycle=not continuation
            )
            cycle = int(record["cycle"])
            await self.store.append_audit(
                job_id,
                "RUN_STARTED" if not continuation else "RUN_RESUMED",
                AgentStatus.RUNNING,
                "Agent entered a bounded execution slice.",
                self._now(),
                details={"cycle": cycle},
            )
            cancel_event = self._cancel_events.setdefault(job_id, asyncio.Event())
            deadline = asyncio.get_running_loop().time() + job.definition.limits.run_timeout_seconds
            last_action = (
                json.loads(record["last_action_json"])
                if record["last_action_json"]
                else None
            )
            start_iteration = int(record["iteration"])

            for offset in range(1, job.definition.limits.max_iterations_per_run + 1):
                if await self._cancelled(job_id, cancel_event):
                    return await self._finish_cancelled(job)
                if asyncio.get_running_loop().time() >= deadline:
                    return await self._finish(
                        job,
                        AgentStatus.TIMED_OUT,
                        "Agent run exceeded its configured timeout.",
                        event="RUN_TIMED_OUT",
                        error="Run timeout",
                    )

                iteration = start_iteration + offset
                try:
                    strategy = await self._strategy_context(job, cancel_event, deadline)
                except asyncio.CancelledError:
                    return await self._finish_cancelled(job)
                except Exception as exc:
                    return await self._finish(
                        job,
                        AgentStatus.FAILED,
                        "Strategy provider failed; no strategy facts were inferred.",
                        event="STRATEGY_PROVIDER_FAILED",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                request = IntelligenceRequest(
                    job_id=job.job_id,
                    objective=job.objective,
                    iteration=iteration,
                    skills=self.skill_discovery.discover(),
                    memory=job.memory,
                    strategy=strategy,
                    last_action_result=last_action,
                )
                try:
                    decision = await self._decide(job, request, cancel_event, deadline)
                    self._validate_untrusted_decision(decision, strategy.availability)
                except asyncio.CancelledError:
                    return await self._finish_cancelled(job)
                except Exception as exc:
                    return await self._finish(
                        job,
                        AgentStatus.FAILED,
                        "Intelligence provider failed; no successful outcome was inferred.",
                        event="PROVIDER_FAILED",
                        error=f"{type(exc).__name__}: {exc}",
                    )

                await self.store.update(job_id, self._now(), iteration=iteration)
                await self.store.append_audit(
                    job_id,
                    "DECISION_ACCEPTED",
                    AgentStatus.RUNNING,
                    decision.summary,
                    self._now(),
                    details={
                        "kind": decision.kind.value,
                        "skill_call": (
                            decision.skill_call.model_dump(mode="json")
                            if decision.skill_call
                            else None
                        ),
                    },
                )

                if decision.kind == DecisionKind.COMPLETE:
                    return await self._finish(
                        job,
                        AgentStatus.SUCCEEDED,
                        decision.summary,
                        event="JOB_SUCCEEDED",
                    )
                if decision.kind == DecisionKind.WAIT:
                    return await self._pause(job, decision.summary, cycle)

                assert decision.skill_call is not None
                try:
                    action_request = ActionRequestFactory.from_assistant(
                        decision.skill_call.model_dump(mode="python"),
                        source=ActionSource.api,
                    )
                except Exception as exc:
                    return await self._finish(
                        job,
                        AgentStatus.FAILED,
                        "Malformed skill call was rejected before execution.",
                        event="SKILL_CALL_REJECTED",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                try:
                    action_result = await self._execute_action(
                        job,
                        action_request,
                        cancel_event,
                        deadline,
                    )
                except asyncio.CancelledError:
                    return await self._finish_cancelled(job)
                except TimeoutError as exc:
                    return await self._finish(
                        job,
                        AgentStatus.TIMED_OUT,
                        "ActionRuntime integration exceeded its bounded timeout.",
                        event="ACTION_TIMED_OUT",
                        error=str(exc),
                    )
                if await self._cancelled(job_id, cancel_event):
                    return await self._finish_cancelled(job)
                last_action = action_result.to_contract_dict()
                await self.store.update(
                    job_id,
                    self._now(),
                    last_action=last_action,
                    pending_action_id=(
                        action_request.id
                        if action_result.status == ActionStatus.CONFIRMATION_REQUIRED
                        else None
                    ),
                    clear_pending_action=(
                        action_result.status != ActionStatus.CONFIRMATION_REQUIRED
                    ),
                )
                await self.store.append_audit(
                    job_id,
                    "ACTION_RESULT",
                    AgentStatus.RUNNING,
                    action_result.summary,
                    self._now(),
                    details={
                        "request_id": str(action_request.id),
                        "status": action_result.status.value,
                        "risk_level": (
                            action_result.risk_level.value
                            if action_result.risk_level
                            else None
                        ),
                        "error": action_result.error,
                    },
                )
                if action_result.status == ActionStatus.CONFIRMATION_REQUIRED:
                    run = await self.store.update(
                        job_id,
                        self._now(),
                        status=AgentStatus.WAITING_CONFIRMATION,
                        summary="Agent paused for ActionRuntime confirmation.",
                        pending_action_id=action_request.id,
                    )
                    await self.store.append_audit(
                        job_id,
                        "WAITING_CONFIRMATION",
                        run.status,
                        run.summary,
                        self._now(),
                        details={"request_id": str(action_request.id)},
                    )
                    return run

            return await self._slice_exhausted(job, cycle)

    async def cancel(self, job_id: UUID) -> AgentRun:
        event = self._cancel_events.setdefault(job_id, asyncio.Event())
        event.set()
        return await self.store.request_cancel(job_id, self._now())

    async def recover(self, job_id: UUID) -> AgentRun:
        record = await self.store.require_record(job_id)
        if record["status"] != AgentStatus.RECOVERY_REQUIRED.value:
            raise AgentConflictError("agent job does not require recovery")
        job = AgentJob.model_validate_json(record["job_json"])
        target = (
            AgentStatus.SCHEDULED
            if job.definition.mode == AgentMode.SCHEDULED
            else AgentStatus.PAUSED
        )
        run = await self.store.update(
            job_id,
            self._now(),
            status=target,
            summary="Interrupted job recovered without assuming prior success.",
            cancel_requested=False,
            error=None,
        )
        await self.store.append_audit(
            job_id, "RECOVERED", target, run.summary, self._now()
        )
        return run

    async def run_due(self, *, limit: int = 100) -> list[AgentRun]:
        results: list[AgentRun] = []
        for job_id in await self.store.due_job_ids(self._now(), limit):
            try:
                results.append(await self.run(job_id))
            except AgentConflictError:
                continue
        return results

    async def _decide(
        self,
        job: AgentJob,
        request: IntelligenceRequest,
        cancel_event: asyncio.Event,
        deadline: float,
    ) -> OrchestratorDecision:
        provider = self.providers.require(job.definition.intelligence_provider)
        last_error: Exception | None = None
        for attempt in range(job.definition.limits.max_provider_retries + 1):
            if cancel_event.is_set():
                raise asyncio.CancelledError
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("run timeout")
            provider_task = asyncio.create_task(provider.decide(request))
            cancel_task = asyncio.create_task(cancel_event.wait())
            try:
                done, _ = await asyncio.wait(
                    {provider_task, cancel_task},
                    timeout=min(job.definition.limits.provider_timeout_seconds, remaining),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_task in done and cancel_task.result():
                    provider_task.cancel()
                    await asyncio.gather(provider_task, return_exceptions=True)
                    raise asyncio.CancelledError
                if provider_task not in done:
                    provider_task.cancel()
                    await asyncio.gather(provider_task, return_exceptions=True)
                    raise TimeoutError("intelligence provider timeout")
                candidate: Any = provider_task.result()
                return OrchestratorDecision.model_validate(candidate)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                await self.store.append_audit(
                    job.job_id,
                    "PROVIDER_ATTEMPT_FAILED",
                    AgentStatus.RUNNING,
                    "Intelligence provider attempt failed.",
                    self._now(),
                    details={"attempt": attempt + 1, "error_type": type(exc).__name__},
                )
            finally:
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)
        assert last_error is not None
        raise last_error

    async def _strategy_context(
        self,
        job: AgentJob,
        cancel_event: asyncio.Event,
        deadline: float,
    ) -> StrategyContext:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("run timeout")
        context_task = asyncio.create_task(
            self.strategy_boundary.context(job.definition.strategy_id, job_id=job.job_id)
        )
        cancel_task = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {context_task, cancel_task},
                timeout=min(job.definition.limits.provider_timeout_seconds, remaining),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done and cancel_task.result():
                context_task.cancel()
                await asyncio.gather(context_task, return_exceptions=True)
                raise asyncio.CancelledError
            if context_task not in done:
                context_task.cancel()
                await asyncio.gather(context_task, return_exceptions=True)
                return StrategyContext(
                    availability=StrategyAvailability.PROVIDER_FAILED,
                    error="TimeoutError: strategy provider timed out",
                )
            return context_task.result()
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)

    async def _execute_action(
        self,
        job: AgentJob,
        action_request: ActionRequest,
        cancel_event: asyncio.Event,
        deadline: float,
    ) -> ActionResult:
        timeout = min(
            job.definition.limits.action_timeout_seconds,
            max(0.001, deadline - asyncio.get_running_loop().time()),
        )
        action_task = asyncio.create_task(
            self.action_runtime.submit(action_request, execution_timeout=timeout)
        )
        cancel_task = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {action_task, cancel_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done and cancel_task.result():
                action_task.cancel()
                await asyncio.gather(action_task, return_exceptions=True)
                raise asyncio.CancelledError
            if action_task not in done:
                action_task.cancel()
                await asyncio.gather(action_task, return_exceptions=True)
                raise TimeoutError("ActionRuntime integration timed out")
            return action_task.result()
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)

    @staticmethod
    def _validate_untrusted_decision(
        decision: OrchestratorDecision, availability: StrategyAvailability
    ) -> None:
        dumped = decision.model_dump(mode="json")
        _reject_authority_claims(dumped)
        assert_secret_free(dumped, label="orchestrator decision")
        if availability == StrategyAvailability.NOT_CONFIGURED and _contains_key(
            decision.output, {"signal", "trade_signal", "tradeSignal"}
        ):
            raise AgentContractError(
                "provider cannot emit a trade signal without StrategyProvider"
            )
        if (
            availability == StrategyAvailability.NOT_CONFIGURED
            and decision.skill_call is not None
            and _looks_like_trade_signal_call(decision.skill_call.skill, decision.skill_call.action)
        ):
            raise AgentContractError(
                "provider cannot request a trade signal without StrategyProvider"
            )

    async def _cancelled(self, job_id: UUID, event: asyncio.Event) -> bool:
        return event.is_set() or await self.store.is_cancel_requested(job_id)

    async def _finish_cancelled(self, job: AgentJob) -> AgentRun:
        return await self._finish(
            job,
            AgentStatus.CANCELLED,
            "Agent job was cancelled; no success was inferred.",
            event="JOB_CANCELLED",
        )

    async def _pause(self, job: AgentJob, summary: str, cycle: int) -> AgentRun:
        if job.definition.mode == AgentMode.ON_DEMAND:
            return await self._finish(
                job,
                AgentStatus.SUCCEEDED,
                summary,
                event="JOB_COMPLETED_WITH_WAIT",
            )
        return await self._reschedule(job, summary, cycle)

    async def _slice_exhausted(self, job: AgentJob, cycle: int) -> AgentRun:
        if job.definition.mode == AgentMode.ON_DEMAND:
            return await self._finish(
                job,
                AgentStatus.EXHAUSTED,
                "Agent reached its iteration limit without completing.",
                event="ITERATION_LIMIT_REACHED",
                error="Iteration limit reached",
            )
        return await self._reschedule(
            job, "Bounded agent slice ended at its iteration limit.", cycle
        )

    async def _reschedule(self, job: AgentJob, summary: str, cycle: int) -> AgentRun:
        if (
            job.definition.mode == AgentMode.CONTINUOUS
            and cycle >= job.definition.limits.max_cycles
        ):
            return await self._finish(
                job,
                AgentStatus.EXHAUSTED,
                "Continuous agent reached its configured lifetime cycle limit.",
                event="CYCLE_LIMIT_REACHED",
                error="Cycle limit reached",
            )
        assert job.definition.interval_seconds is not None
        next_run = self._now() + timedelta(seconds=job.definition.interval_seconds)
        target = (
            AgentStatus.SCHEDULED
            if job.definition.mode == AgentMode.SCHEDULED
            else AgentStatus.PAUSED
        )
        run = await self.store.update(
            job.job_id,
            self._now(),
            status=target,
            summary=summary,
            next_run_at=next_run,
            error=None,
        )
        await self.store.append_audit(
            job.job_id,
            "NEXT_RUN_SCHEDULED",
            target,
            summary,
            self._now(),
            details={"next_run_at": next_run.isoformat(), "cycle": cycle},
        )
        return run

    async def _finish(
        self,
        job: AgentJob,
        status: AgentStatus,
        summary: str,
        *,
        event: str,
        error: str | None = None,
    ) -> AgentRun:
        run = await self.store.update(
            job.job_id,
            self._now(),
            status=status,
            summary=summary,
            clear_pending_action=status != AgentStatus.WAITING_CONFIRMATION,
            error=error,
        )
        await self.store.append_audit(
            job.job_id, event, status, summary, self._now(), details={"error": error}
        )
        self._cancel_events.pop(job.job_id, None)
        return run

    def _lock(self, job_id: UUID) -> asyncio.Lock:
        return self._locks.setdefault(job_id, asyncio.Lock())

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("agent runtime clock must return a timezone-aware datetime")
        return now.astimezone(UTC)


def _reject_authority_claims(value: Any) -> None:
    forbidden_keys = {
        "verified",
        "verification_status",
        "risk",
        "risk_level",
        "authorized",
        "authorization",
        "confirmation_token",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in forbidden_keys:
                raise AgentContractError(
                    f"orchestrator decision cannot claim authority field {key!r}"
                )
            _reject_authority_claims(item)
    elif isinstance(value, list):
        for item in value:
            _reject_authority_claims(item)
    elif isinstance(value, str) and value.upper() == "VERIFIED":
        raise AgentContractError("orchestrator decision cannot claim VERIFIED")


def _contains_key(value: Any, keys: set[str]) -> bool:
    if isinstance(value, dict):
        return any(key in keys or _contains_key(item, keys) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_key(item, keys) for item in value)
    return False


def _looks_like_trade_signal_call(skill: str, action: str) -> bool:
    combined = f"{skill}.{action}".lower()
    return "signal" in combined and any(
        marker in combined for marker in ("trade", "strategy", "market", "signal")
    )
