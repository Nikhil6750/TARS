from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends

from app.config import Settings, get_settings
from app.db import Database
from app.deps import get_db
from app.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/api/v1/health", response_model=HealthResponse)
@router.get("/health", response_model=HealthResponse)
async def health(
    db: Database = Depends(get_db), settings: Settings = Depends(get_settings)
) -> HealthResponse:
    database_status: Literal["ok", "error"]
    try:
        await db.conn.execute("SELECT 1")
        database_status = "ok"
    except Exception:
        database_status = "error"

    return HealthResponse(
        status="ok",
        tars_env=settings.tars_env,
        database=database_status,
        assistant_provider=settings.assistant_provider,
        stt_provider=settings.stt_provider,
        tts_provider=settings.tts_provider,
        # Production wake recognition is transcript matching owned by the
        # turn controller. The configured wake adapter remains dormant.
        wake_word_provider="transcript_matcher",
    )
