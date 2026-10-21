from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, Depends, Request

from app.config import Settings, get_settings
from app.db import Database
from app.deps import get_db
from app.schemas import HealthResponse
from trading.execution_guard import live_trading_enabled

router = APIRouter(tags=["health"])


@router.get("/api/v1/health", response_model=HealthResponse)
@router.get("/health", response_model=HealthResponse)
async def health(
    request: Request,
    db: Database = Depends(get_db), settings: Settings = Depends(get_settings)
) -> HealthResponse:
    database_status: Literal["ok", "error"]
    try:
        await db.conn.execute("SELECT 1")
        database_status = "ok"
    except Exception:
        database_status = "error"

    lane = _voice_lane(request.app.state, settings)
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
        offline_stt_provider=settings.stt_provider,
        offline_tts_provider=settings.tts_provider,
        online_voice_provider=settings.voice_provider,
        live_trading_enabled=live_trading_enabled(),
        **lane,
    )


def _voice_lane(state, settings: Settings) -> dict[str, str]:
    """Same eligibility rule as /ws/realtime: Gemini Live when configured, online and not backed off."""
    mode = str(getattr(settings, "voice_mode", "auto")).lower()
    connectivity = getattr(state, "connectivity", None)
    online = connectivity.online() if connectivity is not None else not getattr(settings, "force_offline", False)
    if settings.voice_provider.lower() != "gemini_live":
        gemini = "NOT_CONFIGURED"
    elif not settings.gemini_api_key:
        gemini = "UNHEALTHY: GEMINI_API_KEY is not set"
    elif not online:
        gemini = "UNHEALTHY: offline"
    elif time.monotonic() < getattr(state, "gemini_unavailable_until", 0):
        gemini = "UNHEALTHY: " + str(getattr(state, "gemini_unavailable_reason", None) or "backed off")
    else:
        gemini = "HEALTHY"
    session = getattr(state, "realtime_session", None)
    if session is not None:
        active = "gemini_live" if type(session).__name__ == "GeminiLiveVoiceSession" else "local_streaming"
    elif mode == "offline" or (mode == "auto" and gemini != "HEALTHY"):
        active = "idle (next session: local_streaming)"
    else:
        active = "idle (next session: gemini_live)"
    return {"voice_mode": mode, "active_voice_lane": active, "gemini_live_health": gemini}
