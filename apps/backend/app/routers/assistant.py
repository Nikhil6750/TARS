from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request

from app.contracts import ContractValidationError, validate_assistant_message
from app.deps import get_assistant_router
from assistant.router import AssistantRouter

router = APIRouter(tags=["assistant"])


@router.post("/api/v1/assistant/query")
@router.post("/api/assistant/messages")
@router.post("/api/v1/assistant/messages")
async def assistant_query(
    request: Request,
    assistant_router: AssistantRouter = Depends(get_assistant_router),
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

    reply = await assistant_router.handle_text(
        text=text,
        conversation_id=str(conversation_id) if conversation_id else str(uuid4()),
    )
    return reply.assistant_message.to_contract_dict()
