from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)

from actions.errors import (
    ActionNotFoundError,
    AssistantActionError,
    ConfirmationReplayError,
    DuplicateActionError,
    InvalidConfirmationError,
)
from actions.requests import ActionRequestFactory, DeterministicActionRouter
from actions.runtime import ActionRuntime
from app.action_contracts import ActionRequest, ActionSource
from app.contracts import ContractValidationError, validate_action_request
from app.deps import get_action_runtime

router = APIRouter(tags=["actions"])


@router.get("/api/v1/actions/capabilities")
async def action_capabilities(
    runtime: ActionRuntime = Depends(get_action_runtime),
) -> dict[str, Any]:
    return {"skills": runtime.registry.describe()}


@router.get("/api/v1/actions/audit")
async def recent_action_audit(
    limit: int = Query(default=100, ge=1, le=500),
    runtime: ActionRuntime = Depends(get_action_runtime),
) -> list[dict[str, Any]]:
    return await runtime.store.list_recent_audit(limit)


@router.post("/api/v1/actions")
async def submit_action(
    request: Request,
    runtime: ActionRuntime = Depends(get_action_runtime),
) -> dict[str, Any]:
    raw = await _json_object(request, "Action payload")
    try:
        validate_action_request(raw)
        action_request = ActionRequest.model_validate(raw)
        result = await runtime.submit(action_request)
    except ContractValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DuplicateActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        # Pydantic errors and canonical-model parse errors are input failures.
        if exc.__class__.__module__.startswith("pydantic"):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise
    return result.to_contract_dict()


@router.post("/api/v1/actions/assistant")
async def submit_assistant_action(
    request: Request,
    runtime: ActionRuntime = Depends(get_action_runtime),
) -> dict[str, Any]:
    raw = await _json_object(request, "Assistant action envelope")
    allowed = {"proposal", "source", "active_context"}
    if set(raw) - allowed or "proposal" not in raw or "source" not in raw:
        raise HTTPException(
            status_code=422,
            detail="Envelope requires proposal and source, with optional active_context only",
        )
    try:
        source = ActionSource(raw["source"])
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid action source") from exc
    if source == ActionSource.deterministic:
        raise HTTPException(
            status_code=422,
            detail="Assistant proposals cannot claim deterministic origin",
        )
    try:
        action_request = ActionRequestFactory.from_assistant(
            raw["proposal"],
            source=source,
            active_context=raw.get("active_context"),
        )
        result = await runtime.submit(action_request)
    except AssistantActionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result.to_contract_dict()


@router.post("/api/v1/actions/resolve")
async def resolve_deterministic_action(
    request: Request,
    runtime: ActionRuntime = Depends(get_action_runtime),
) -> dict[str, Any]:
    raw = await _json_object(request, "Deterministic action envelope")
    if set(raw) - {"text", "active_context"} or not isinstance(raw.get("text"), str):
        raise HTTPException(
            status_code=422, detail="Envelope requires text, with optional active_context only"
        )
    try:
        action_request = DeterministicActionRouter().resolve(
            raw["text"], active_context=raw.get("active_context")
        )
    except AssistantActionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if action_request is None:
        raise HTTPException(status_code=422, detail="No deterministic action matched")
    result = await runtime.submit(action_request)
    return result.to_contract_dict()


@router.post("/api/v1/actions/{request_id}/confirm")
async def confirm_action(
    request_id: UUID,
    request: Request,
    runtime: ActionRuntime = Depends(get_action_runtime),
) -> dict[str, Any]:
    raw = await _json_object(request, "Confirmation payload")
    if set(raw) != {"confirmation_token", "approved"}:
        raise HTTPException(
            status_code=422,
            detail="Confirmation requires confirmation_token and approved only",
        )
    if not isinstance(raw["confirmation_token"], str) or not isinstance(
        raw["approved"], bool
    ):
        raise HTTPException(status_code=422, detail="Invalid confirmation payload")
    try:
        result = await runtime.confirm(
            request_id, raw["confirmation_token"], raw["approved"]
        )
    except ActionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidConfirmationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ConfirmationReplayError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result.to_contract_dict()


@router.get("/api/v1/actions/{request_id}/audit")
async def action_audit(
    request_id: UUID,
    runtime: ActionRuntime = Depends(get_action_runtime),
) -> list[dict[str, Any]]:
    try:
        return await runtime.get_audit(request_id)
    except ActionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/v1/actions/{request_id}")
async def action_result(
    request_id: UUID,
    runtime: ActionRuntime = Depends(get_action_runtime),
) -> dict[str, Any]:
    try:
        result = await runtime.get_result(request_id)
    except ActionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return result.to_contract_dict()


@router.websocket("/ws/actions")
async def action_stream(websocket: WebSocket) -> None:
    manager = websocket.app.state.action_ws_manager
    await manager.connect(websocket)
    try:
        await websocket.send_json({"type": "action_stream_ready"})
        while True:
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        manager.disconnect(websocket)


async def _json_object(request: Request, label: str) -> dict[str, Any]:
    try:
        raw: Any = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Invalid JSON payload") from exc
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail=f"{label} must be a JSON object")
    return raw
