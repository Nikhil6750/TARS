from __future__ import annotations

import base64
import binascii
import json
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import StreamingResponse

from app.contracts import ContractValidationError, validate_assistant_message
from app.deps import get_chart_analysis_service, get_orchestrator
from assistant.chart_analysis import ChartAnalysisError, ChartAnalysisService
from assistant.errors import AssistantProviderError
from orchestrator.orchestrator import TarsOrchestrator

router = APIRouter(tags=["assistant"])

_MAX_IMAGE_BYTES = 15 * 1_048_576


@router.post("/api/v1/assistant/query")
@router.post("/api/assistant/messages")
@router.post("/api/v1/assistant/messages")
async def assistant_query(
    request: Request,
    orchestrator: TarsOrchestrator = Depends(get_orchestrator),
) -> dict:
    try:
        raw: Any = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON payload: {exc}") from exc

    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="Request payload must be a JSON object")

    # If payload presents as a canonical message or has schema fields, validate strictly
    if "schema_version" in raw or "role" in raw or "input_mode" in raw or "message_id" in raw:
        try:
            validate_assistant_message(raw)
        except ContractValidationError as exc:
            # Generic error detail to prevent any secret reflection
            raise HTTPException(
                status_code=422,
                detail=f"Schema validation failed: {exc}",
            ) from exc

        if raw.get("role") != "user":
            raise HTTPException(status_code=422, detail="Message role must be 'user'")

        text = raw.get("content", "")
        conversation_id = raw.get("conversation_id")
    else:
        text = raw.get("text", "")
        conversation_id = raw.get("conversation_id")

    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=422, detail="Query text must be a non-empty string")

    reply = await orchestrator.handle_text(
        text=text,
        conversation_id=str(conversation_id) if conversation_id else str(uuid4()),
    )
    return reply.assistant_message.to_contract_dict()


@router.post("/api/v1/assistant/analyze-chart")
async def analyze_chart(
    request: Request,
    chart_analysis: ChartAnalysisService = Depends(get_chart_analysis_service),
) -> dict[str, Any]:
    """"Analyze this chart": takes a screenshot the frontend already
    captured through the real, backend-authorized capture flow (see
    windows_app.capture_active_window / actions/frontend_bridge.py -- this
    endpoint does not perform its own capture) and the active-window
    context alongside it, and returns a structured, uncertainty-aware read
    from Claude. Never a validated quant_brain signal; see
    assistant/chart_analysis.py's ChartAnalysisResult.disclaimer."""
    try:
        raw: Any = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON payload: {exc}") from exc
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="Request payload must be a JSON object")

    capture = raw.get("capture")
    if not isinstance(capture, dict):
        raise HTTPException(status_code=422, detail="'capture' must be an object")

    image_data = capture.get("image_data_base64")
    if not isinstance(image_data, str) or not image_data.strip():
        raise HTTPException(
            status_code=422, detail="capture.image_data_base64 must be a non-empty string"
        )
    if capture.get("error"):
        raise HTTPException(
            status_code=422,
            detail=f"Capture reported an error, nothing to analyze: {capture['error']}",
        )
    if capture.get("is_secure_desktop"):
        raise HTTPException(
            status_code=422,
            detail="Capture was refused (secure desktop); nothing to analyze",
        )

    image_format = str(capture.get("image_format") or "image/png")
    _, _, encoded = image_data.partition(",") if image_data.startswith("data:") else ("", "", image_data)
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail=f"capture.image_data_base64 is not valid base64: {exc}"
        ) from exc
    if len(image_bytes) > _MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Captured image exceeds the size limit")

    active_context = raw.get("active_context")
    active_context_text = _describe_active_context(active_context) if isinstance(active_context, dict) else ""

    conversation_id = raw.get("conversation_id")
    raw_goal = raw.get("goal")
    goal_text = raw_goal.strip() if isinstance(raw_goal, str) and raw_goal.strip() else "Analyze this chart."

    try:
        result = await chart_analysis.analyze(
            image_bytes=image_bytes,
            image_format=image_format,
            conversation_id=str(conversation_id) if conversation_id else str(uuid4()),
            active_context_text=active_context_text,
            goal_text=goal_text,
        )
    except ChartAnalysisError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AssistantProviderError as exc:
        raise HTTPException(status_code=502, detail=f"Chart analysis provider failed: {exc}") from exc

    return result.to_dict()


@router.post("/api/v1/assistant/analyze-chart/stream")
async def analyze_chart_stream(
    request: Request,
    chart_analysis: ChartAnalysisService = Depends(get_chart_analysis_service),
) -> StreamingResponse:
    try:
        raw: Any = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON payload: {exc}") from exc
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="Request payload must be a JSON object")

    capture = raw.get("capture")
    if not isinstance(capture, dict):
        raise HTTPException(status_code=422, detail="'capture' must be an object")

    image_data = capture.get("image_data_base64")
    if not isinstance(image_data, str) or not image_data.strip():
        raise HTTPException(
            status_code=422, detail="capture.image_data_base64 must be a non-empty string"
        )
    if capture.get("error"):
        raise HTTPException(
            status_code=422,
            detail=f"Capture reported an error, nothing to analyze: {capture['error']}",
        )
    if capture.get("is_secure_desktop"):
        raise HTTPException(
            status_code=422,
            detail="Capture was refused (secure desktop); nothing to analyze",
        )

    image_format = str(capture.get("image_format") or "image/png")
    _, _, encoded = image_data.partition(",") if image_data.startswith("data:") else ("", "", image_data)
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail=f"capture.image_data_base64 is not valid base64: {exc}"
        ) from exc
    if len(image_bytes) > _MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Captured image exceeds the size limit")

    active_context = raw.get("active_context")
    active_context_text = _describe_active_context(active_context) if isinstance(active_context, dict) else ""

    conversation_id = raw.get("conversation_id")
    raw_goal = raw.get("goal")
    goal_text = raw_goal.strip() if isinstance(raw_goal, str) and raw_goal.strip() else "Analyze this chart."

    async def event_generator():
        try:
            async for item in chart_analysis.analyze_stream(
                image_bytes=image_bytes,
                image_format=image_format,
                conversation_id=str(conversation_id) if conversation_id else str(uuid4()),
                active_context_text=active_context_text,
                goal_text=goal_text,
            ):
                yield f"data: {json.dumps(item)}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'detail': str(exc)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def _describe_active_context(context: dict[str, Any]) -> str:
    parts = []
    executable = context.get("executable")
    title = context.get("window_title")
    if executable:
        parts.append(f"active application: {executable}")
    if title:
        parts.append(f"window title: {title}")
    return "; ".join(parts)
