"""Latency diagnostics — read-only percentile view over `request_traces`
(app/latency_store.py). Not user-facing: the HUD/voice UI never calls this;
it exists for the same reason /api/v1/runtime/readiness does (a
developer-facing view of real backend state), and returns nothing normal
end-user text/voice output would show. TARS Alexa-Speed Phase A.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.deps import get_latency_trace_store
from app.latency_store import LatencyTraceStore

router = APIRouter(tags=["diagnostics"])


@router.get("/api/v1/diagnostics/latency")
async def latency_percentiles(
    kind: str = Query("chart_analysis"),
    limit: int = Query(200, ge=1, le=2000),
    trace_store: LatencyTraceStore = Depends(get_latency_trace_store),
) -> dict:
    return await trace_store.percentiles(kind, limit=limit)
