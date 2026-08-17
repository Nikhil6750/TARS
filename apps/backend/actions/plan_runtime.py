from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from actions.plan_models import (
    TERMINAL_PLAN_STATUSES,
    ActionPlan,
    ActionStep,
    PlanExecution,
    PlanStatus,
    StepStatus,
    StructuredObservation,
    VerificationRecord,
    VerificationStatus,
)
from actions.plan_store import PlanStore
from actions.runtime import ActionRuntime
from actions.safety import (
    SensitiveContext,
    audit_safe_arguments,
    contains_secret_field,
    detect_sensitive_context,
    redact_sensitive,
)
from app.action_contracts import ActionRequest, ActionSource, ActionStatus, RiskLevel


class PlanRuntimeError(RuntimeError):
    pass


class PlanNotFoundError(PlanRuntimeError):
    pass


class PlanConflictError(PlanRuntimeError):
    pass


class PlanValidationError(ValueError):
    pass


_EXECUTABLE_KEYS = {
    "code",
    "command",
    "script",
    "shell",
    "powershell",
    "javascript",
    "executable_code",
}
_FORGED_VERIFICATION_KEYS = {"verification", "verification_status", "verified"}
_SENSITIVE_BLOCKING = {
    SensitiveContext.PASSWORD_INPUT,
    SensitiveContext.CREDENTIAL_DIALOG,
    SensitiveContext.SECURE_DESKTOP,
    SensitiveContext.SYSTEM_CRITICAL,
}


class PlanRuntime:
    """Synchronous, bounded orchestration over the authoritative ActionRuntime.

    One caller-visible transition executes at a time. There are no detached
    tasks: confirmations and observations pause the plan and must arrive via a
    subsequent explicit request.
    """

    def __init__(
        self,
        store: PlanStore,
        action_runtime: ActionRuntime,
        *,
        max_steps: int = 12,
        max_retries: int = 2,
        max_reobservations: int = 2,
        plan_timeout: timedelta = timedelta(minutes=5),
        plan_max_age: timedelta = timedelta(minutes=5),
        observation_max_age: timedelta = timedelta(minutes=2),
    ) -> None:
        if min(max_steps, max_retries + 1, max_reobservations + 1) <= 0:
            raise ValueError("Plan runtime bounds must be positive")
        if min(plan_timeout, plan_max_age, observation_max_age) <= timedelta(0):
            raise ValueError("Plan runtime time bounds must be positive")
        self.store = store
        self.action_runtime = action_runtime
        self.max_steps = max_steps
        self.max_retries = max_retries
        self.max_reobservations = max_reobservations
        self.plan_timeout = plan_timeout
        self.plan_max_age = plan_max_age
        self.observation_max_age = observation_max_age
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._cancel_events: dict[UUID, asyncio.Event] = {}

    async def initialize(self) -> None:
        await self.store.initialize()

    async def submit(self, plan: ActionPlan) -> PlanExecution:
        now = self._now()
        effective_risks = await self._validate(plan, now)
        await self.store.insert(
            plan,
            effective_risks=effective_risks,
            deadline=now + self.plan_timeout,
            now=now,
        )
        await self.store.append_audit(
            plan.plan_id,
            "PLAN_CREATED",
            now,
            details={
                "goal": plan.goal,
                "plan": _audit_safe_plan_dict(plan),
                "provenance": plan.provenance.value,
            },
        )
        for step in plan.steps:
            await self.store.append_audit(
                plan.plan_id,
                "PERMISSION_CLASSIFIED",
                now,
                step_id=step.step_id,
                details={
                    "proposed_risk": step.risk_level,
                    "effective_risk": effective_risks[str(step.step_id)],
                },
            )
        self._cancel_events[plan.plan_id] = asyncio.Event()
        return await self._execute_current(plan.plan_id)

    async def get(self, plan_id: UUID) -> PlanExecution:
        record = await self.store.get(plan_id)
        if record is None:
            raise PlanNotFoundError(f"Action plan {plan_id} was not found")
        return self.store.execution_from_record(record)

    async def get_audit(self, plan_id: UUID) -> list[dict[str, Any]]:
        if await self.store.get(plan_id) is None:
            raise PlanNotFoundError(f"Action plan {plan_id} was not found")
        return await self.store.list_audit(plan_id)

    async def cancel(self, plan_id: UUID) -> PlanExecution:
        record = await self.store.get(plan_id)
        if record is None:
            raise PlanNotFoundError(f"Action plan {plan_id} was not found")
        status = PlanStatus(record["status"])
        if status in TERMINAL_PLAN_STATUSES:
            return self.store.execution_from_record(record)
        now = self._now()
        statuses = json.loads(record["step_statuses_json"])
        index = int(record["current_step_index"])
        plan = ActionPlan.model_validate_json(record["plan_json"])
        if index < len(plan.steps):
            statuses[str(plan.steps[index].step_id)] = StepStatus.CANCELLED.value
        await self.store.update(
            plan_id,
            now,
            status=PlanStatus.CANCELLED.value,
            step_statuses_json=_dump(statuses),
            error="Plan cancelled",
        )
        self._cancel_events.setdefault(plan_id, asyncio.Event()).set()
        active_request_id = record["active_request_id"]
        if active_request_id:
            request = await self.action_runtime.store.get_request(active_request_id)
            if request is not None:
                await self.action_runtime.fail_incomplete(
                    request, "Execution interrupted because its plan was cancelled"
                )
        await self.store.append_audit(plan_id, "PLAN_CANCELLED", now)
        return await self.get(plan_id)

    async def confirm(
        self,
        plan_id: UUID,
        *,
        step_id: UUID,
        request_id: UUID,
        token: str,
        approved: bool,
    ) -> PlanExecution:
        async with self._lock(plan_id):
            record, plan, step = await self._require_current(
                plan_id, PlanStatus.WAITING_CONFIRMATION, step_id, request_id
            )
            now = self._now()
            try:
                self._ensure_current_time(record, now)
            except PlanConflictError:
                return await self._time_out(record, now)
            await self.store.append_audit(
                plan_id,
                "CONFIRMATION_DECISION",
                now,
                step_id=step.step_id,
                request_id=request_id,
                details={"approved": approved},
            )
            result = await self._run_confirmation(
                plan_id,
                request_id=request_id,
                token=token,
                approved=approved,
                deadline_raw=record["deadline"],
            )
            latest = await self.store.get(plan_id)
            if latest is None:
                raise PlanNotFoundError(f"Action plan {plan_id} was not found")
            if PlanStatus(latest["status"]) in TERMINAL_PLAN_STATUSES:
                return self.store.execution_from_record(latest)
            if not approved:
                return await self._terminate_step(
                    record,
                    plan,
                    step,
                    PlanStatus.FAILED,
                    StepStatus.FAILED,
                    "Confirmation was declined",
                    "PLAN_FAILED",
                )
            return await self._handle_action_result(record, plan, step, result)

    async def observe(self, observation: StructuredObservation) -> PlanExecution:
        if _contains_key(observation.state, _FORGED_VERIFICATION_KEYS):
            raise PlanValidationError(
                "Observation may contain state only; verification is computed by the backend"
            )
        async with self._lock(observation.plan_id):
            record, plan, step = await self._require_current(
                observation.plan_id,
                PlanStatus.WAITING_OBSERVATION,
                observation.step_id,
                observation.request_id,
            )
            now = self._now()
            self._validate_observation_time(observation, now)
            try:
                self._ensure_current_time(record, now)
            except PlanConflictError:
                return await self._time_out(record, now)
            await self.store.insert_observation(observation, now)
            sensitive = detect_sensitive_context(observation.state)
            verification = _verify(step.expected_result, observation, now)
            await self.store.append_audit(
                plan.plan_id,
                "OBSERVATION_RECORDED",
                now,
                step_id=step.step_id,
                request_id=observation.request_id,
                details={
                    "source": observation.source.value,
                    "observation_id": str(observation.observation_id),
                    "state_fields": sorted(str(name) for name in observation.state),
                    "sensitive_context": sorted(item.value for item in sensitive),
                },
            )
            await self.store.append_audit(
                plan.plan_id,
                "VERIFICATION",
                now,
                step_id=step.step_id,
                request_id=observation.request_id,
                details={
                    "observation_id": str(verification.observation_id),
                    "status": verification.status.value,
                    "reason": verification.reason,
                    "expected_fields": sorted(str(name) for name in step.expected_result),
                },
            )
            await self.store.update(
                plan.plan_id,
                now,
                verification_json=verification.model_dump_json(),
                latest_sensitive_json=_dump(sorted(item.value for item in sensitive)),
            )
            if verification.status == VerificationStatus.VERIFIED:
                statuses = json.loads(record["step_statuses_json"])
                statuses[str(step.step_id)] = StepStatus.VERIFIED.value
                next_index = int(record["current_step_index"]) + 1
                if next_index >= len(plan.steps):
                    await self.store.update(
                        plan.plan_id,
                        now,
                        status=PlanStatus.COMPLETED.value,
                        current_step_index=next_index,
                        step_statuses_json=_dump(statuses),
                        active_request_id=None,
                        error=None,
                    )
                    await self.store.append_audit(plan.plan_id, "PLAN_COMPLETED", now)
                    return await self.get(plan.plan_id)
                await self.store.update(
                    plan.plan_id,
                    now,
                    status=PlanStatus.RUNNING.value,
                    current_step_index=next_index,
                    step_statuses_json=_dump(statuses),
                    active_request_id=None,
                    error=None,
                )
                return await self._execute_current_locked(plan.plan_id)

            if verification.status == VerificationStatus.UNKNOWN:
                reobservations = json.loads(record["reobservations_json"])
                count = int(reobservations.get(str(step.step_id), 0))
                if step.recovery.allow_reobserve and count < self.max_reobservations:
                    reobservations[str(step.step_id)] = count + 1
                    await self.store.update(
                        plan.plan_id,
                        now,
                        reobservations_json=_dump(reobservations),
                    )
                    await self.store.append_audit(
                        plan.plan_id,
                        "REOBSERVE_REQUIRED",
                        now,
                        step_id=step.step_id,
                        details={"attempt": count + 1, "reason": verification.reason},
                    )
                    return await self.get(plan.plan_id)

            if step.recovery.allow_retry and verification.status == VerificationStatus.FAILED:
                attempts = json.loads(record["attempts_json"])
                if int(attempts.get(str(step.step_id), 0)) <= self.max_retries:
                    await self.store.append_audit(
                        plan.plan_id,
                        "RETRY",
                        now,
                        step_id=step.step_id,
                        details={"reason": verification.reason},
                    )
                    await self.store.update(
                        plan.plan_id,
                        now,
                        status=PlanStatus.RUNNING.value,
                        active_request_id=None,
                    )
                    return await self._execute_current_locked(plan.plan_id)

            return await self._terminate_step(
                record,
                plan,
                step,
                PlanStatus.FAILED,
                StepStatus.FAILED,
                f"Verification {verification.status.value}: {verification.reason}",
                "PLAN_FAILED",
            )

    async def _validate(self, plan: ActionPlan, now: datetime) -> dict[str, str]:
        if len(plan.steps) > self.max_steps:
            raise PlanValidationError(f"Action plan exceeds {self.max_steps} steps")
        if plan.status != PlanStatus.PLANNED:
            raise PlanValidationError("New action plan must have PLANNED status")
        created = plan.created_at
        if created.tzinfo is None or created.utcoffset() is None:
            raise PlanValidationError("created_at must include a timezone")
        if created.astimezone(UTC) > now + timedelta(seconds=30):
            raise PlanValidationError("Action plan timestamp is too far in the future")
        if created.astimezone(UTC) < now - self.plan_max_age:
            raise PlanValidationError("Action plan timestamp is stale")
        if contains_secret_field(plan.context):
            raise PlanValidationError("Secrets and credentials are not allowed in action plans")

        effective: dict[str, str] = {}
        for step in plan.steps:
            if step.skill == "terminal" or _contains_key(step.arguments, _EXECUTABLE_KEYS):
                raise PlanValidationError("Arbitrary executable code is not allowed in action plans")
            if contains_secret_field(step.arguments):
                raise PlanValidationError("Secrets and credentials are not allowed in action plans")
            skill = self.action_runtime.registry.get(step.skill)
            if skill is None:
                raise PlanValidationError(f"Unknown skill: {step.skill}")
            if step.action not in skill.capabilities:
                raise PlanValidationError(f"Unsupported capability: {step.skill}.{step.action}")
            try:
                await skill.validate(step.action, step.arguments)
                for alternate in step.recovery.alternate_arguments:
                    if contains_secret_field(alternate) or _contains_key(
                        alternate, _EXECUTABLE_KEYS
                    ):
                        raise PlanValidationError("Unsafe alternate arguments in recovery policy")
                    await skill.validate(step.action, alternate)
            except PlanValidationError:
                raise
            except Exception as exc:
                raise PlanValidationError(
                    f"Invalid arguments for {step.skill}.{step.action}: {exc}"
                ) from exc
            risk = self.action_runtime.permission_engine.classify(
                skill, step.action, step.arguments
            )
            effective[str(step.step_id)] = risk.value
        return effective

    async def _execute_current(self, plan_id: UUID) -> PlanExecution:
        async with self._lock(plan_id):
            return await self._execute_current_locked(plan_id)

    async def _execute_current_locked(self, plan_id: UUID) -> PlanExecution:
        record = await self.store.get(plan_id)
        if record is None:
            raise PlanNotFoundError(f"Action plan {plan_id} was not found")
        if PlanStatus(record["status"]) in TERMINAL_PLAN_STATUSES:
            return self.store.execution_from_record(record)
        now = self._now()
        try:
            self._ensure_current_time(record, now)
        except PlanConflictError:
            return await self._time_out(record, now)

        plan = ActionPlan.model_validate_json(record["plan_json"])
        index = int(record["current_step_index"])
        if index >= len(plan.steps):
            raise PlanConflictError("Plan has no current step")
        step = plan.steps[index]
        statuses = json.loads(record["step_statuses_json"])
        if any(statuses[str(dep)] != StepStatus.VERIFIED.value for dep in step.dependencies):
            return await self._terminate_step(
                record,
                plan,
                step,
                PlanStatus.FAILED,
                StepStatus.FAILED,
                "Step dependencies are not verified",
                "DEPENDENCY_FAILED",
            )

        risk = RiskLevel(json.loads(record["effective_risks_json"])[str(step.step_id)])
        sensitive = detect_sensitive_context(plan.context) | {
            SensitiveContext(item) for item in json.loads(record["latest_sensitive_json"])
        }
        if risk == RiskLevel.BLOCKED or (risk != RiskLevel.READ_ONLY and sensitive & _SENSITIVE_BLOCKING):
            reason = (
                "Step blocked by deterministic permission policy"
                if risk == RiskLevel.BLOCKED
                else "State-changing step blocked in a sensitive context"
            )
            return await self._terminate_step(
                record,
                plan,
                step,
                PlanStatus.BLOCKED,
                StepStatus.BLOCKED,
                reason,
                "STEP_BLOCKED",
            )

        attempts = json.loads(record["attempts_json"])
        attempt = int(attempts.get(str(step.step_id), 0)) + 1
        if attempt > self.max_retries + 1:
            return await self._terminate_step(
                record,
                plan,
                step,
                PlanStatus.FAILED,
                StepStatus.FAILED,
                "Retry limit exhausted",
                "RETRY_EXHAUSTED",
            )
        attempts[str(step.step_id)] = attempt
        arguments = self._arguments_for_attempt(step, attempt)
        skill = self.action_runtime.registry.get(step.skill)
        if skill is None:
            raise PlanConflictError(f"Skill became unavailable: {step.skill}")
        attempt_risk = self.action_runtime.permission_engine.classify(
            skill, step.action, arguments
        )
        if attempt_risk == RiskLevel.BLOCKED or (
            attempt_risk != RiskLevel.READ_ONLY and sensitive & _SENSITIVE_BLOCKING
        ):
            return await self._terminate_step(
                record,
                plan,
                step,
                PlanStatus.BLOCKED,
                StepStatus.BLOCKED,
                "Recovery action was blocked by current safety policy",
                "STEP_BLOCKED",
            )
        request = ActionRequest(
            skill=step.skill,
            action=step.action,
            arguments=arguments,
            source=ActionSource.api,
        )
        statuses[str(step.step_id)] = StepStatus.RUNNING.value
        await self.store.update(
            plan_id,
            now,
            status=PlanStatus.RUNNING.value,
            step_statuses_json=_dump(statuses),
            attempts_json=_dump(attempts),
            active_request_id=str(request.id),
            verification_json=None,
            error=None,
        )
        await self.store.append_audit(
            plan_id,
            "ACTION_PROPOSED",
            now,
            step_id=step.step_id,
            request_id=request.id,
            details={
                "skill": step.skill,
                "action": step.action,
                "arguments": audit_safe_arguments(arguments),
                "attempt": attempt,
                "effective_risk": attempt_risk.value,
            },
        )
        result = await self._run_action(plan_id, request, record["deadline"])
        latest = await self.store.get(plan_id)
        if latest is None:
            raise PlanNotFoundError(f"Action plan {plan_id} was not found")
        if PlanStatus(latest["status"]) in TERMINAL_PLAN_STATUSES:
            return self.store.execution_from_record(latest)
        return await self._handle_action_result(latest, plan, step, result)

    async def _run_action(
        self, plan_id: UUID, request: ActionRequest, deadline_raw: str
    ) -> Any:
        deadline = datetime.fromisoformat(deadline_raw)
        remaining = max(0.0, (deadline - self._now()).total_seconds())
        task = asyncio.create_task(
            self.action_runtime.submit(request, execution_timeout=max(0.001, remaining))
        )
        cancel_event = self._cancel_events.setdefault(plan_id, asyncio.Event())
        cancel_wait = asyncio.create_task(cancel_event.wait())
        done, _ = await asyncio.wait(
            {task, cancel_wait},
            timeout=remaining + 0.1,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            cancel_wait.cancel()
            result = await task
            if self._now() >= deadline:
                record = await self.store.get(plan_id)
                if record is not None and PlanStatus(record["status"]) not in TERMINAL_PLAN_STATUSES:
                    await self._time_out(record, self._now())
            return result
        task.cancel()
        try:
            result = await task
        except asyncio.CancelledError:
            result = await self.action_runtime.fail_incomplete(
                request, "Execution interrupted by plan cancellation or timeout"
            )
        cancel_wait.cancel()
        if cancel_wait not in done:
            record = await self.store.get(plan_id)
            if record is not None and PlanStatus(record["status"]) not in TERMINAL_PLAN_STATUSES:
                await self._time_out(record, self._now())
        return result

    async def _run_confirmation(
        self,
        plan_id: UUID,
        *,
        request_id: UUID,
        token: str,
        approved: bool,
        deadline_raw: str,
    ) -> Any:
        deadline = datetime.fromisoformat(deadline_raw)
        remaining = max(0.0, (deadline - self._now()).total_seconds())
        task = asyncio.create_task(
            self.action_runtime.confirm(
                request_id,
                token,
                approved,
                execution_timeout=max(0.001, remaining),
            )
        )
        cancel_event = self._cancel_events.setdefault(plan_id, asyncio.Event())
        cancel_wait = asyncio.create_task(cancel_event.wait())
        done, _ = await asyncio.wait(
            {task, cancel_wait},
            timeout=remaining + 0.1,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            cancel_wait.cancel()
            result = await task
            if self._now() >= deadline:
                record = await self.store.get(plan_id)
                if record is not None and PlanStatus(record["status"]) not in TERMINAL_PLAN_STATUSES:
                    await self._time_out(record, self._now())
            return result
        task.cancel()
        try:
            result = await task
        except asyncio.CancelledError:
            request = await self.action_runtime.store.get_request(request_id)
            if request is None:
                raise PlanConflictError(
                    "Confirmed action disappeared during interruption"
                ) from None
            result = await self.action_runtime.fail_incomplete(
                request, "Confirmed action interrupted by plan cancellation or timeout"
            )
        cancel_wait.cancel()
        if cancel_wait not in done:
            record = await self.store.get(plan_id)
            if record is not None and PlanStatus(record["status"]) not in TERMINAL_PLAN_STATUSES:
                await self._time_out(record, self._now())
        return result

    async def _handle_action_result(
        self, record: dict[str, Any], plan: ActionPlan, step: ActionStep, result: Any
    ) -> PlanExecution:
        now = self._now()
        await self.store.append_audit(
            plan.plan_id,
            "ACTION_RESULT",
            now,
            step_id=step.step_id,
            request_id=result.request_id,
            details={
                "status": result.status.value,
                "risk_level": result.risk_level.value if result.risk_level else None,
                "summary": result.summary,
                "error": result.error,
            },
        )
        statuses = json.loads(record["step_statuses_json"])
        if result.status == ActionStatus.CONFIRMATION_REQUIRED:
            statuses[str(step.step_id)] = StepStatus.WAITING_CONFIRMATION.value
            await self.store.update(
                plan.plan_id,
                now,
                status=PlanStatus.WAITING_CONFIRMATION.value,
                step_statuses_json=_dump(statuses),
            )
            await self.store.append_audit(
                plan.plan_id,
                "PLAN_PAUSED_FOR_CONFIRMATION",
                now,
                step_id=step.step_id,
                request_id=result.request_id,
                details={
                    "skill": step.skill,
                    "action": step.action,
                    "arguments": audit_safe_arguments(step.arguments),
                },
            )
            execution = await self.get(plan.plan_id)
            pending = dict(execution.pending_operation or {})
            if "confirmation_token" in result.data:
                pending["confirmation_token"] = result.data["confirmation_token"]
            if "confirmation_expires_at" in result.data:
                pending["confirmation_expires_at"] = result.data[
                    "confirmation_expires_at"
                ]
            return execution.model_copy(update={"pending_operation": pending})
        if result.status == ActionStatus.SUCCEEDED:
            statuses[str(step.step_id)] = StepStatus.WAITING_OBSERVATION.value
            await self.store.update(
                plan.plan_id,
                now,
                status=PlanStatus.WAITING_OBSERVATION.value,
                step_statuses_json=_dump(statuses),
            )
            return await self.get(plan.plan_id)
        if result.status == ActionStatus.BLOCKED:
            return await self._terminate_step(
                record,
                plan,
                step,
                PlanStatus.BLOCKED,
                StepStatus.BLOCKED,
                result.error or "Action was blocked",
                "STEP_BLOCKED",
            )

        attempts = json.loads(record["attempts_json"])
        if step.recovery.allow_retry and int(attempts.get(str(step.step_id), 0)) <= self.max_retries:
            await self.store.append_audit(
                plan.plan_id,
                "RETRY",
                now,
                step_id=step.step_id,
                request_id=result.request_id,
                details={"reason": result.error or result.summary},
            )
            await self.store.update(
                plan.plan_id, now, status=PlanStatus.RUNNING.value, active_request_id=None
            )
            return await self._execute_current_locked(plan.plan_id)
        return await self._terminate_step(
            record,
            plan,
            step,
            PlanStatus.FAILED,
            StepStatus.FAILED,
            result.error or "Action failed",
            "PLAN_FAILED",
        )

    async def _terminate_step(
        self,
        record: dict[str, Any],
        plan: ActionPlan,
        step: ActionStep,
        plan_status: PlanStatus,
        step_status: StepStatus,
        error: str,
        event: str,
    ) -> PlanExecution:
        now = self._now()
        latest = await self.store.get(plan.plan_id) or record
        statuses = json.loads(latest["step_statuses_json"])
        statuses[str(step.step_id)] = step_status.value
        await self.store.update(
            plan.plan_id,
            now,
            status=plan_status.value,
            step_statuses_json=_dump(statuses),
            error=error,
        )
        await self.store.append_audit(
            plan.plan_id, event, now, step_id=step.step_id, details={"error": error}
        )
        return await self.get(plan.plan_id)

    async def _time_out(self, record: dict[str, Any], now: datetime) -> PlanExecution:
        plan = ActionPlan.model_validate_json(record["plan_json"])
        index = int(record["current_step_index"])
        statuses = json.loads(record["step_statuses_json"])
        if index < len(plan.steps):
            statuses[str(plan.steps[index].step_id)] = StepStatus.FAILED.value
        await self.store.update(
            plan.plan_id,
            now,
            status=PlanStatus.TIMED_OUT.value,
            step_statuses_json=_dump(statuses),
            error="Plan timeout expired",
        )
        await self.store.append_audit(plan.plan_id, "PLAN_TIMED_OUT", now)
        return await self.get(plan.plan_id)

    async def _require_current(
        self,
        plan_id: UUID,
        required_status: PlanStatus,
        step_id: UUID,
        request_id: UUID,
    ) -> tuple[dict[str, Any], ActionPlan, ActionStep]:
        record = await self.store.get(plan_id)
        if record is None:
            raise PlanNotFoundError(f"Action plan {plan_id} was not found")
        if record["status"] != required_status.value:
            raise PlanConflictError(f"Plan is not {required_status.value}")
        plan = ActionPlan.model_validate_json(record["plan_json"])
        index = int(record["current_step_index"])
        if index >= len(plan.steps) or plan.steps[index].step_id != step_id:
            raise PlanConflictError("Step is not the current plan step")
        if record["active_request_id"] != str(request_id):
            raise PlanConflictError("Action request is not active for this step")
        return record, plan, plan.steps[index]

    def _arguments_for_attempt(self, step: ActionStep, attempt: int) -> dict[str, Any]:
        alternate_index = attempt - 2
        if 0 <= alternate_index < len(step.recovery.alternate_arguments):
            return step.recovery.alternate_arguments[alternate_index]
        return step.arguments

    def _ensure_current_time(self, record: dict[str, Any], now: datetime) -> None:
        deadline = datetime.fromisoformat(record["deadline"])
        if now >= deadline:
            raise PlanConflictError("Plan timeout expired")

    def _validate_observation_time(
        self, observation: StructuredObservation, now: datetime
    ) -> None:
        observed = observation.observed_at
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise PlanValidationError("Observation timestamp must include a timezone")
        observed = observed.astimezone(UTC)
        if observed < now - self.observation_max_age:
            raise PlanValidationError("Observation timestamp is stale")
        if observed > now + timedelta(seconds=30):
            raise PlanValidationError("Observation timestamp is too far in the future")

    def _lock(self, plan_id: UUID) -> asyncio.Lock:
        return self._locks.setdefault(plan_id, asyncio.Lock())

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)


def _verify(
    expected: dict[str, Any], observation: StructuredObservation, now: datetime
) -> VerificationRecord:
    if not expected:
        status = VerificationStatus.UNKNOWN
        reason = "No expected state was supplied"
    else:
        comparison = _compare_expected(expected, observation.state)
        if comparison is None:
            status = VerificationStatus.VERIFIED
            reason = "Observed state matches every expected field"
        elif comparison.startswith("missing"):
            status = VerificationStatus.UNKNOWN
            reason = comparison
        else:
            status = VerificationStatus.FAILED
            reason = comparison
    return VerificationRecord(
        observation_id=observation.observation_id,
        expected_state=redact_sensitive(expected),
        observed_state=redact_sensitive(observation.state),
        status=status,
        reason=reason,
        verified_at=now,
    )


def _compare_expected(expected: Any, observed: Any, path: str = "state") -> str | None:
    if isinstance(expected, Mapping):
        if not isinstance(observed, Mapping):
            return f"mismatch at {path}"
        for key, value in expected.items():
            if key not in observed:
                return f"missing expected field {path}.{key}"
            mismatch = _compare_expected(value, observed[key], f"{path}.{key}")
            if mismatch:
                return mismatch
        return None
    if isinstance(expected, Sequence) and not isinstance(expected, (str, bytes, bytearray)):
        if expected != observed:
            return f"mismatch at {path}"
        return None
    return None if expected == observed else f"mismatch at {path}"


def _contains_key(value: Any, forbidden: set[str]) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(name).lower() in forbidden or _contains_key(item, forbidden)
            for name, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_key(item, forbidden) for item in value)
    return False


def _dump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def _audit_safe_plan_dict(plan: ActionPlan) -> dict[str, Any]:
    """A plan's JSON dump for PLAN_CREATED audit, with each step's arguments
    (and recovery.alternate_arguments) redacted the same way a single
    ActionRequest's arguments are -- a plan step can carry a credential into
    a text field exactly like a standalone action can."""
    dumped = plan.model_dump(mode="json")
    for step in dumped.get("steps", []):
        step["arguments"] = audit_safe_arguments(step.get("arguments", {}))
        recovery = step.get("recovery") or {}
        alternates = recovery.get("alternate_arguments")
        if isinstance(alternates, list):
            recovery["alternate_arguments"] = [
                audit_safe_arguments(alt) if isinstance(alt, dict) else alt for alt in alternates
            ]
    return dumped
