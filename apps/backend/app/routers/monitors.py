"""Read-only monitor status for the HUD, and the labelled demo replay controls."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(tags=["monitors"])


def _manager(request: Request):
    manager = getattr(request.app.state, "monitors", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="Monitors are not running")
    return manager


@router.get("/api/v1/monitors/status")
async def monitor_status(request: Request):
    return await _manager(request).status()


@router.post("/api/v1/demo/replay/start")
async def start_replay(request: Request):
    return await _manager(request).start_replay()


@router.post("/api/v1/demo/replay/stop")
async def stop_replay(request: Request):
    await _manager(request).stop_replay()
    return {"stopped": True}
