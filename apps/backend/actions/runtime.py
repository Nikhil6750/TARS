from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from actions.errors import (
    ActionNotFoundError,
    ConfirmationReplayError,
    InvalidConfirmationError,
)
from actions.permissions import PermissionEngine
from actions.registry import SkillRegistry
from actions.store import ActionStore
from app.action_contracts import (
    TERMINAL_STATUSES,
    ActionRequest,
    ActionResult,
    ActionStatus,
    ActiveWindowContext,
    RiskLevel,
    SkillExecutionError,
    SkillValidationError,
)
from app.contracts import (
    ContractValidationError,
    validate_action_request,
    validate_action_result,
)
from app.ws_manager import ConnectionManager


class ActionRuntime:
    def __init__(
        self,
        store: ActionStore,
        registry: SkillRegistry,
        *,
        permission_engine: PermissionEngine | None = None,
        broadcaster: ConnectionManager | None = None,
        request_max_age: timedelta = timedelta(minutes=5),
        future_tolerance: timedelta = timedelta(seconds=30),
        confirmation_ttl: timedelta = timedelta(minutes=2),
        execution_timeout: float = 30.0,
        clock: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.permission_engine = permission_engine or PermissionEngine()
        self.broadcaster = broadcaster
        self.request_max_age = request_max_age
        self.future_tolerance = future_tolerance
        self.confirmation_ttl = confirmation_ttl
        if execution_timeout <= 0:
            raise ValueError("execution_timeout must be positive")
        self.execution_timeout = execution_timeout
        self._clock = clock or (lambda: datetime.now(UTC))
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        await self.store.initialize()

    async def submit(
        self,
        request: ActionRequest,
        *,
        active_context: ActiveWindowContext | None = None,
        execution_timeout: float | None = None,
    ) -> ActionResult:
        if active_context is not None and request.active_context is None:
            request = request.model_copy(update={"active_context": active_context})
        validate_action_request(request.to_contract_dict())

        async with self._lock:
            now = self._now()
            pending = self._result(
                request,
                ActionStatus.PENDING,
                "Action request received and awaiting deterministic evaluation.",
                now=now,
            )
            await self.store.insert_request(request, pending, now)
            await self.store.append_audit(
                request.id,
                "REQUESTED",
                pending,
                now,
                details={
                    "skill": request.skill,
                    "action": request.action,
                    "source": request.source.value,
                    "arguments": request.arguments,
                    "active_context": (
                        request.active_context.model_dump(mode="json")
                        if request.active_context
                        else None
                    ),
                },
            )

            expiry_error = self._request_expiry_error(request, now)
            if expiry_error:
                result = self._result(
                    request,
                    ActionStatus.DENIED,
                    "Action request was denied because its timestamp is not current.",
                    risk=RiskLevel.BLOCKED,
                    error=expiry_error,
                    now=now,
                )
                return await self._finish(result, "REQUEST_EXPIRED", now)

            skill = self.registry.get(request.skill)
            if skill is None:
                result = self._result(
                    request,
                    ActionStatus.DENIED,
                    f"Action denied: skill {request.skill!r} is not registered.",
                    risk=RiskLevel.BLOCKED,
                    error="Unknown skill",
                    now=now,
                )
                return await self._finish(result, "UNKNOWN_SKILL", now)

            if request.action not in skill.capabilities:
                result = self._result(
                    request,
                    ActionStatus.DENIED,
                    "Action denied because the registered skill does not expose that capability.",
                    risk=RiskLevel.BLOCKED,
                    error="Unsupported skill capability",
                    now=now,
                )
                return await self._finish(result, "VALIDATION_DENIED", now)

            try:
                await skill.validate(request.action, request.arguments)
            except (SkillValidationError, ValueError, TypeError) as exc:
                result = self._result(
                    request,
                    ActionStatus.DENIED,
                    "Action denied because its action or arguments are invalid.",
                    risk=RiskLevel.BLOCKED,
                    error=str(exc),
                    now=now,
                )
                return await self._finish(result, "VALIDATION_DENIED", now)
            except Exception as exc:
                result = self._result(
                    request,
                    ActionStatus.FAILED,
                    "Action validation failed unexpectedly; nothing was executed.",
                    risk=RiskLevel.BLOCKED,
                    error=f"Validation failure: {type(exc).__name__}",
                    now=now,
                )
                return await self._finish(result, "VALIDATION_FAILED", now)

            risk = self.permission_engine.classify(skill, request.action, request.arguments)
            if risk == RiskLevel.BLOCKED:
                result = self._result(
                    request,
                    ActionStatus.BLOCKED,
                    "Action blocked by deterministic permission policy; nothing was executed.",
                    risk=risk,
                    error="This operation is blocked by policy",
                    now=now,
                )
                return await self._finish(result, "BLOCKED", now)

            if risk == RiskLevel.CONFIRM_REQUIRED:
                return await self._require_confirmation(request, risk, now)

            return await self._execute(
                request, skill, risk, now, execution_timeout=execution_timeout
            )

    async def confirm(
        self,
        request_id: UUID,
        token: str,
        approved: bool,
        *,
        execution_timeout: float | None = None,
    ) -> ActionResult:
        async with self._lock:
            now = self._now()
            record = await self.store.get_record(request_id)
            if record is None:
                raise ActionNotFoundError(f"Action request {request_id} was not found")
            if (
                record["status"] != ActionStatus.CONFIRMATION_REQUIRED.value
                or record["confirmation_consumed"]
            ):
                raise ConfirmationReplayError("Confirmation was already consumed or is not pending")

            expected_hash = record["confirmation_token_hash"] or ""
            if not isinstance(token, str) or not token or not hmac.compare_digest(
                expected_hash, _token_hash(token)
            ):
                current = ActionResult.model_validate_json(record["result_json"])
                await self.store.append_audit(
                    request_id,
                    "CONFIRMATION_TOKEN_REJECTED",
                    current,
                    now,
                    details={"reason": "token_mismatch"},
                )
                raise InvalidConfirmationError("Invalid confirmation token")

            expires_at = _parse_datetime(record["confirmation_expires_at"])
            request = ActionRequest.model_validate_json(record["request_json"])
            if expires_at is None or now > expires_at:
                await self.store.mark_confirmation_consumed(request_id, now)
                result = self._result(
                    request,
                    ActionStatus.DENIED,
                    "Action denied because confirmation expired before approval.",
                    risk=RiskLevel.CONFIRM_REQUIRED,
                    error="Confirmation expired",
                    now=now,
                )
                return await self._finish(result, "CONFIRMATION_EXPIRED", now, consume=True)

            claimed = await self.store.mark_confirmation_consumed(request_id, now)
            if not claimed:
                raise ConfirmationReplayError("Confirmation was already consumed")

            if not approved:
                result = self._result(
                    request,
                    ActionStatus.DENIED,
                    "Action denied by the user; nothing was executed.",
                    risk=RiskLevel.CONFIRM_REQUIRED,
                    error="User declined confirmation",
                    now=now,
                )
                return await self._finish(result, "CONFIRMATION_DENIED", now, consume=True)

            skill = self.registry.get(request.skill)
            if skill is None:
                result = self._result(
                    request,
                    ActionStatus.FAILED,
                    "Confirmed action could not run because its skill is unavailable.",
                    risk=RiskLevel.CONFIRM_REQUIRED,
                    error="Skill unavailable after confirmation",
                    now=now,
                )
                return await self._finish(result, "FAILED", now, consume=True)

            # Revalidate and reclassify at execution time. Confirmation never freezes
            # or bypasses policy, and a BLOCKED action remains impossible to execute.
            try:
                await skill.validate(request.action, request.arguments)
            except Exception as exc:
                result = self._result(
                    request,
                    ActionStatus.FAILED,
                    "Confirmed action failed revalidation; nothing was executed.",
                    risk=RiskLevel.CONFIRM_REQUIRED,
                    error=f"Revalidation failed: {exc}",
                    now=now,
                )
                return await self._finish(result, "FAILED", now, consume=True)
            risk = self.permission_engine.classify(skill, request.action, request.arguments)
            if risk == RiskLevel.BLOCKED:
                result = self._result(
                    request,
                    ActionStatus.BLOCKED,
                    "Confirmed action is blocked by current deterministic policy.",
                    risk=risk,
                    error="Confirmation cannot override blocked policy",
                    now=now,
                )
                return await self._finish(result, "BLOCKED", now, consume=True)
            if risk != RiskLevel.CONFIRM_REQUIRED:
                # A changed classification is fail-closed rather than executing under
                # permission different from the one the user reviewed.
                result = self._result(
                    request,
                    ActionStatus.DENIED,
                    "Action classification changed after confirmation; resubmit it.",
                    risk=risk,
                    error="Risk classification changed",
                    now=now,
                )
                return await self._finish(result, "CLASSIFICATION_CHANGED", now, consume=True)

            await self.store.append_audit(
                request.id,
                "CONFIRMATION_ACCEPTED",
                self._result(
                    request,
                    ActionStatus.RUNNING,
                    "User confirmation accepted; action execution started.",
                    risk=risk,
                    now=now,
                ),
                now,
            )
            return await self._execute(
                request,
                skill,
                risk,
                now,
                consume=True,
                execution_timeout=execution_timeout,
            )

    async def get_result(self, request_id: UUID) -> ActionResult:
        result = await self.store.get_result(request_id)
        if result is None:
            raise ActionNotFoundError(f"Action request {request_id} was not found")
        return result

    async def get_audit(self, request_id: UUID) -> list[dict[str, Any]]:
        if await self.store.get_record(request_id) is None:
            raise ActionNotFoundError(f"Action request {request_id} was not found")
        return await self.store.list_audit(request_id)

    async def fail_incomplete(self, request: ActionRequest, error: str) -> ActionResult:
        """Close an interrupted action without inventing a successful outcome."""

        async with self._lock:
            current = await self.store.get_result(request.id)
            if current is None:
                return self._result(
                    request,
                    ActionStatus.FAILED,
                    "Action did not complete.",
                    risk=RiskLevel.BLOCKED,
                    error=error,
                    now=self._now(),
                )
            if current.status in TERMINAL_STATUSES:
                return current
            result = self._result(
                request,
                ActionStatus.FAILED,
                "Action did not complete.",
                risk=current.risk_level or RiskLevel.BLOCKED,
                error=error,
                now=self._now(),
            )
            return await self._finish(result, "INTERRUPTED", self._now())

    async def _require_confirmation(
        self, request: ActionRequest, risk: RiskLevel, now: datetime
    ) -> ActionResult:
        token = self._token_factory()
        expires_at = now + self.confirmation_ttl
        stored = self._result(
            request,
            ActionStatus.CONFIRMATION_REQUIRED,
            "Explicit confirmation is required before this action can run.",
            risk=risk,
            data={
                "skill": request.skill,
                "action": request.action,
                "arguments": request.arguments,
                "confirmation_expires_at": expires_at.isoformat(),
            },
            now=now,
        )
        await self.store.set_result(
            stored,
            now,
            confirmation_token_hash=_token_hash(token),
            confirmation_expires_at=expires_at,
        )
        await self.store.append_audit(request.id, "CONFIRMATION_REQUIRED", stored, now)
        outward = stored.model_copy(
            update={"data": {**stored.data, "confirmation_token": token}}
        )
        await self._broadcast(outward)
        return outward

    async def _execute(
        self,
        request: ActionRequest,
        skill: Any,
        risk: RiskLevel,
        now: datetime,
        *,
        consume: bool = False,
        execution_timeout: float | None = None,
    ) -> ActionResult:
        running = self._result(
            request,
            ActionStatus.RUNNING,
            "Action execution started.",
            risk=risk,
            now=now,
        )
        await self.store.set_result(running, now, consume_confirmation=consume)
        await self.store.append_audit(request.id, "RUNNING", running, now)
        await self._broadcast(running)
        try:
            candidate = await asyncio.wait_for(
                skill.execute(request),
                timeout=execution_timeout or self.execution_timeout,
            )
            result = self._normalize_skill_result(request, candidate, risk, now)
        except TimeoutError:
            result = self._result(
                request,
                ActionStatus.FAILED,
                "Action execution timed out.",
                risk=risk,
                error="Execution timeout",
                now=self._now(),
            )
        except asyncio.CancelledError:
            result = self._result(
                request,
                ActionStatus.FAILED,
                "Action execution was cancelled.",
                risk=risk,
                error="Execution cancelled",
                now=self._now(),
            )
        except SkillExecutionError as exc:
            result = self._result(
                request,
                ActionStatus.FAILED,
                "Action execution failed.",
                risk=risk,
                error=str(exc),
                now=now,
            )
        except Exception as exc:
            result = self._result(
                request,
                ActionStatus.FAILED,
                "Action execution failed unexpectedly.",
                risk=risk,
                error=f"Execution failure: {type(exc).__name__}",
                now=now,
            )
        event = "SUCCEEDED" if result.status == ActionStatus.SUCCEEDED else "FAILED"
        return await self._finish(result, event, self._now(), consume=consume)

    def _normalize_skill_result(
        self,
        request: ActionRequest,
        candidate: Any,
        risk: RiskLevel,
        now: datetime,
    ) -> ActionResult:
        if not isinstance(candidate, ActionResult):
            return self._invalid_skill_result(request, risk, now, "non-ActionResult response")
        if candidate.request_id != request.id:
            return self._invalid_skill_result(request, risk, now, "request_id mismatch")
        if candidate.status not in {ActionStatus.SUCCEEDED, ActionStatus.FAILED}:
            return self._invalid_skill_result(request, risk, now, "non-terminal execution status")
        if candidate.status == ActionStatus.SUCCEEDED and candidate.error is not None:
            return self._invalid_skill_result(request, risk, now, "successful result included error")
        if candidate.status == ActionStatus.FAILED and not candidate.error:
            return self._invalid_skill_result(request, risk, now, "failed result omitted error")

        normalized = candidate.model_copy(
            update={
                "risk_level": risk,
                "completed_at": candidate.completed_at or now,
            }
        )
        try:
            validate_action_result(normalized.to_contract_dict())
        except ContractValidationError:
            return self._invalid_skill_result(request, risk, now, "contract-invalid result")
        return normalized

    def _invalid_skill_result(
        self, request: ActionRequest, risk: RiskLevel, now: datetime, reason: str
    ) -> ActionResult:
        return self._result(
            request,
            ActionStatus.FAILED,
            "Skill returned an invalid execution result.",
            risk=risk,
            error=f"Invalid skill result: {reason}",
            now=now,
        )

    async def _finish(
        self,
        result: ActionResult,
        event: str,
        now: datetime,
        *,
        consume: bool = False,
    ) -> ActionResult:
        validate_action_result(result.to_contract_dict())
        await self.store.set_result(result, now, consume_confirmation=consume)
        await self.store.append_audit(result.request_id, event, result, now)
        await self._broadcast(result)
        return result

    async def _broadcast(self, result: ActionResult) -> None:
        if self.broadcaster is not None:
            await self.broadcaster.broadcast(
                {"type": "action_result", "result": result.to_contract_dict()}
            )

    def _request_expiry_error(self, request: ActionRequest, now: datetime) -> str | None:
        requested_at = request.requested_at
        if requested_at.tzinfo is None or requested_at.utcoffset() is None:
            return "requested_at must include a timezone"
        requested_at = requested_at.astimezone(UTC)
        if requested_at < now - self.request_max_age:
            return "Action request expired"
        if requested_at > now + self.future_tolerance:
            return "Action request timestamp is too far in the future"
        return None

    def _result(
        self,
        request: ActionRequest,
        status: ActionStatus,
        summary: str,
        *,
        risk: RiskLevel | None = None,
        data: dict[str, Any] | None = None,
        error: str | None = None,
        now: datetime,
    ) -> ActionResult:
        return ActionResult(
            request_id=request.id,
            status=status,
            risk_level=risk,
            summary=summary,
            data=data or {},
            error=error,
            started_at=now,
            completed_at=now if status in TERMINAL_STATUSES else None,
        )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("Action runtime clock must return a timezone-aware datetime")
        return now.astimezone(UTC)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)
