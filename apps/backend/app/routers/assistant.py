from __future__ import annotations

from fastapi import APIRouter, Depends

from app.deps import get_assistant_router
from app.schemas import AssistantMessage, TextQueryRequest
from assistant.router import AssistantRouter

router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])


@router.post("/query")
async def query(
    body: TextQueryRequest,
    assistant_router: AssistantRouter = Depends(get_assistant_router),
) -> AssistantMessage:
    reply = await assistant_router.handle_text(
        text=body.text,
        conversation_id=str(body.conversation_id) if body.conversation_id else None,
    )
    return reply.assistant_message
