"""Desktop-lifecycle endpoints the Rust supervisor (apps/web/src-tauri)
calls -- graceful shutdown, watcher pause/resume (the SAME
MarketWatchService pause state the voice tools already use, mission
section 6: "Do not create another pause flag"), and post-sleep rebase
(mission section 11: RESUME_REBASE).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends

from app.deps import get_daily_brief_service, get_market_watch_service, get_monitors
from app.supervisor_hooks import request_graceful_shutdown

if TYPE_CHECKING:
    from monitors.manager import MonitorManager
    from trading.daily_brief import DailyMarketBriefService
    from trading.market_watch_service import MarketWatchService

router = APIRouter(tags=["lifecycle"])


@router.post("/api/v1/runtime/shutdown")
async def shutdown() -> dict:
    """Cooperative shutdown: sets uvicorn's own should_exit flag, which
    runs app/main.py's lifespan `finally` block (clean watcher/monitor/db
    teardown) before the process exits. The caller (the Rust supervisor)
    treats a response from this endpoint as "intentional" and must not
    auto-restart on the process exit that follows."""
    requested = request_graceful_shutdown()
    return {"status": "shutting_down" if requested else "no_server_reference"}


@router.post("/api/v1/watcher/pause")
async def pause_watcher(service: MarketWatchService = Depends(get_market_watch_service)) -> dict:
    await service.pause()
    return await service.status()


@router.post("/api/v1/watcher/resume")
async def resume_watcher(service: MarketWatchService = Depends(get_market_watch_service)) -> dict:
    await service.resume()
    return await service.status()


@router.get("/api/v1/watcher/status")
async def watcher_status(service: MarketWatchService = Depends(get_market_watch_service)) -> dict:
    return await service.status()


@router.post("/api/v1/runtime/resume_rebase")
async def resume_rebase(
    service: MarketWatchService = Depends(get_market_watch_service),
    monitors: MonitorManager = Depends(get_monitors),
) -> dict:
    """Called by the Rust supervisor after it detects a system sleep/
    resume gap (mission section 11) -- discards stale price/evidence
    baselines so the watcher doesn't compare a pre-sleep price to a
    post-resume one and fire a false "giant move" alert. Never touches
    the persisted watchlist or pause state."""
    service.rebase(reason="system_resume")
    mt5 = getattr(monitors, "mt5", None)
    if mt5 is not None:
        mt5.rebase()
    return {"status": "rebased"}


@router.get("/api/v1/runtime/daily_brief_status")
async def daily_brief_status(service: DailyMarketBriefService = Depends(get_daily_brief_service)) -> dict:
    """Read-only: does NOT generate anything, just reports whether
    today's brief has already been sent (for a desktop UI to show)."""
    return await service.status()
