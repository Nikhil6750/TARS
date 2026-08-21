"""Receiving endpoint for the native BackgroundChartWatcher
(apps/web/src-tauri/src/chart_watcher.rs) -- TARS Alexa-Speed Phase C.

Not called by the frontend/webview at all: the Rust watcher POSTs here
directly from its own background OS thread, the same "independent of the
React panel's lifecycle" pattern wake_engine.rs already uses for
transcription. See assistant/chart_watch.py's ChartWatchService for the
actual cooldown/freshness policy -- this router is just request parsing.
"""
from __future__ import annotations

import base64
import binascii
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from app.deps import get_chart_watch_service
from assistant.chart_watch import ChartWatchService

router = APIRouter(tags=["chart-watch"])

_MAX_IMAGE_BYTES = 15 * 1_048_576


@router.post("/api/v1/chart-watch/frame")
async def chart_watch_frame(
    request: Request,
    chart_watch: ChartWatchService = Depends(get_chart_watch_service),
) -> dict:
    try:
        raw: Any = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON payload: {exc}") from exc
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="Request payload must be a JSON object")

    chart_window_id = raw.get("chart_window_id")
    if not isinstance(chart_window_id, str) or not chart_window_id.strip():
        raise HTTPException(status_code=422, detail="chart_window_id must be a non-empty string")

    image_data = raw.get("image_data_base64")
    if not isinstance(image_data, str) or not image_data.strip():
        raise HTTPException(status_code=422, detail="image_data_base64 must be a non-empty string")

    image_format = str(raw.get("image_format") or "image/bmp")
    trigger_reason = str(raw.get("trigger_reason") or "unspecified")

    _, _, encoded = image_data.partition(",") if image_data.startswith("data:") else ("", "", image_data)
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"image_data_base64 is not valid base64: {exc}") from exc
    if len(image_bytes) > _MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Captured frame exceeds the size limit")

    outcome = await chart_watch.handle_frame(
        chart_window_id=chart_window_id,
        image_bytes=image_bytes,
        image_format=image_format,
        trigger_reason=trigger_reason,
    )
    return {
        "action": outcome.action,
        "chart_window_id": outcome.chart_window_id,
        "identity": (
            {
                "chart_window_id": outcome.identity.chart_window_id,
                "symbol": outcome.identity.symbol,
                "timeframe": outcome.identity.timeframe,
            }
            if outcome.identity
            else None
        ),
        "error": outcome.error,
    }
