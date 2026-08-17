from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from actions.errors import (
    ActionNotFoundError,
    ConfirmationReplayError,
    InvalidConfirmationError,
)
from actions.plan_models import ActionPlan, StructuredObservation
from actions.plan_requests import ActionPlanFactory
from actions.plan_runtime import (
    PlanConflictError,
    PlanNotFoundError,
    PlanRuntime,
    PlanValidationError,
)
from actions.plan_store import DuplicateObservationError, DuplicatePlanError
from app.deps import get_plan_runtime

router = APIRouter(prefix="/api/v1/action-plans", tags=["action-plans"])


@router.post("")
async def submit_plan(
    request: Request, runtime: PlanRuntime = Depends(get_plan_runtime)
) -> dict[str, Any]:
    raw = await _json_object(request)
    try:
        plan = ActionPlan.model_validate(raw)
        execution = await runtime.submit(plan)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PlanValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DuplicatePlanError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return execution.model_dump(mode="json")


@router.post("/assistant")
async def submit_assistant_plan(
    request: Request, runtime: PlanRuntime = Depends(get_plan_runtime)
) -> dict[str, Any]:
    raw = await _json_object(request)
    if set(raw) != {"proposal"}:
        raise HTTPException(status_code=422, detail="Envelope requires proposal only")
    try:
        execution = await runtime.submit(ActionPlanFactory.from_assistant(raw["proposal"]))
    except PlanValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return execution.model_dump(mode="json")


@router.get("/{plan_id}")
async def get_plan(
    plan_id: UUID, runtime: PlanRuntime = Depends(get_plan_runtime)
) -> dict[str, Any]:
    try:
        return (await runtime.get(plan_id)).model_dump(mode="json")
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{plan_id}/audit")
async def get_plan_audit(
    plan_id: UUID, runtime: PlanRuntime = Depends(get_plan_runtime)
) -> list[dict[str, Any]]:
    try:
        return await runtime.get_audit(plan_id)
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{plan_id}/cancel")
async def cancel_plan(
    plan_id: UUID, runtime: PlanRuntime = Depends(get_plan_runtime)
) -> dict[str, Any]:
    try:
        return (await runtime.cancel(plan_id)).model_dump(mode="json")
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{plan_id}/confirm")
async def confirm_plan_step(
    plan_id: UUID,
    request: Request,
    runtime: PlanRuntime = Depends(get_plan_runtime),
) -> dict[str, Any]:
    raw = await _json_object(request)
    required = {"step_id", "request_id", "confirmation_token", "approved"}
    if set(raw) != required or not isinstance(raw.get("approved"), bool):
        raise HTTPException(status_code=422, detail="Invalid plan confirmation payload")
    try:
        execution = await runtime.confirm(
            plan_id,
            step_id=UUID(str(raw["step_id"])),
            request_id=UUID(str(raw["request_id"])),
            token=raw["confirmation_token"],
            approved=raw["approved"],
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid plan confirmation payload") from exc
    except (PlanNotFoundError, ActionNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidConfirmationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (PlanConflictError, ConfirmationReplayError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return execution.model_dump(mode="json")


@router.post("/{plan_id}/observations")
async def submit_observation(
    plan_id: UUID,
    request: Request,
    runtime: PlanRuntime = Depends(get_plan_runtime),
) -> dict[str, Any]:
    raw = await _json_object(request)
    try:
        observation = StructuredObservation.model_validate(raw)
        if observation.plan_id != plan_id:
            raise PlanValidationError("Observation plan_id does not match route")
        execution = await runtime.observe(observation)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PlanValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PlanConflictError, DuplicateObservationError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return execution.model_dump(mode="json")


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        raw: Any = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Invalid JSON payload") from exc
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="Payload must be a JSON object")
    return raw
