from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends

from app.config import Settings, get_settings
from app.db import Database
from app.deps import get_db, get_market_watch_service, get_monitors, get_voice_providers
from app.readiness import build_readiness_report
from app.voice_state import VoiceProviders

if TYPE_CHECKING:
    from monitors.manager import MonitorManager
    from trading.market_watch_service import MarketWatchService

router = APIRouter(tags=["runtime"])


@router.get("/api/v1/runtime/readiness")
async def readiness(
    db: Database = Depends(get_db),
    voice: VoiceProviders = Depends(get_voice_providers),
    settings: Settings = Depends(get_settings),
    monitors: MonitorManager = Depends(get_monitors),
    watcher: MarketWatchService = Depends(get_market_watch_service),
) -> dict:
    try:
        await db.conn.execute("SELECT 1")
        database_ok = True
    except Exception:
        database_ok = False

    report = await build_readiness_report(settings, voice, database_ok=database_ok)
    # Additive only -- the existing assistant/stt/tts/wake/database/
    # claude_cli/ready/message shape (and its tests) is untouched; these
    # keys compose already-truthful, already-maintained state (mission:
    # "Do not call everything READY merely because processes exist") from
    # MonitorManager/MarketWatchService rather than inventing a second
    # source of truth for any of it.
    payload = report.to_dict()
    monitor_status = await monitors.status()
    payload["mt5"] = monitor_status.get("mt5")
    payload["calendar"] = monitor_status.get("calendar")
    payload["news"] = monitor_status.get("news")
    payload["tradingview"] = monitor_status.get("tradingview")
    payload["watcher"] = await watcher.status()
    return payload
