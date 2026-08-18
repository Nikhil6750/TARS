from __future__ import annotations

from fastapi import APIRouter, Depends

from app.config import Settings, get_settings
from app.db import Database
from app.deps import get_db, get_voice_providers
from app.readiness import build_readiness_report
from app.voice_state import VoiceProviders

router = APIRouter(tags=["runtime"])


@router.get("/api/v1/runtime/readiness")
async def readiness(
    db: Database = Depends(get_db),
    voice: VoiceProviders = Depends(get_voice_providers),
    settings: Settings = Depends(get_settings),
) -> dict:
    try:
        await db.conn.execute("SELECT 1")
        database_ok = True
    except Exception:
        database_ok = False

    report = await build_readiness_report(settings, voice, database_ok=database_ok)
    return report.to_dict()
