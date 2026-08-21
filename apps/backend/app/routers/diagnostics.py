"""Latency/provider diagnostics — read-only views over `request_traces`
(app/latency_store.py). Not user-facing: the HUD/voice UI never calls this;
it exists for the same reason /api/v1/runtime/readiness does (a
developer-facing view of real backend state), and returns nothing normal
end-user text/voice output would show. TARS Alexa-Speed Phases A and G.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.deps import get_latency_trace_store
from app.latency_store import LatencyTraceStore
from assistant.provider_health import ProviderHealth, ProviderHealthTracker

router = APIRouter(tags=["diagnostics"])


@router.get("/api/v1/diagnostics/latency")
async def latency_percentiles(
    kind: str = Query("chart_analysis"),
    limit: int = Query(200, ge=1, le=2000),
    trace_store: LatencyTraceStore = Depends(get_latency_trace_store),
) -> dict:
    return await trace_store.percentiles(kind, limit=limit)


@router.get("/api/v1/diagnostics/provider-health")
async def provider_health(
    provider_id: str = Query(...),
    kind: str = Query("assistant_text"),
    limit: int = Query(200, ge=1, le=2000),
    trace_store: LatencyTraceStore = Depends(get_latency_trace_store),
) -> dict:
    tracker = ProviderHealthTracker(trace_store)
    health: ProviderHealth = await tracker.health_for(provider_id, kind=kind, limit=limit)
    return {
        "provider_id": health.provider_id,
        "sample_size": health.sample_size,
        "error_count": health.error_count,
        "success_rate": health.success_rate,
        "p50_ms": health.p50_ms,
        "p90_ms": health.p90_ms,
        "p95_ms": health.p95_ms,
        "max_ms": health.max_ms,
        "last_error": health.last_error,
    }
