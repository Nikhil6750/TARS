"""Persistence for per-request latency telemetry (`request_traces`, see
storage/migrations/0005_request_traces.sql). Reuses `app/latency.py`'s
`LatencyTracker` shape and `assistant/provider.py`'s `ProviderDiagnostics`
rather than inventing a third timing representation -- this module is only
the missing "actually write it down" step, plus the read-side percentile
query a diagnostics endpoint needs.

Same access pattern as every other *Service class in this backend
(EventService, ConversationStore, etc.): wraps the one shared aiosqlite
connection from app/db.py, no separate pool.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiosqlite


@dataclass
class RequestTrace:
    request_id: str
    kind: str
    conversation_id: str | None = None
    provider_id: str | None = None
    warm_path: bool = False
    started_at: str = ""
    completed_at: str | None = None
    capture_ms: float | None = None
    provider_start_ms: float | None = None
    first_token_ms: float | None = None
    provider_latency_ms: float | None = None
    total_ms: float | None = None
    error: str | None = None


class LatencyTraceStore:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    async def record(self, trace: RequestTrace) -> None:
        completed_at = trace.completed_at or datetime.now(UTC).isoformat()
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO request_traces (
                request_id, kind, conversation_id, provider_id, warm_path,
                started_at, completed_at, capture_ms, provider_start_ms,
                first_token_ms, provider_latency_ms, total_ms, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trace.request_id,
                trace.kind,
                trace.conversation_id,
                trace.provider_id,
                1 if trace.warm_path else 0,
                trace.started_at,
                completed_at,
                trace.capture_ms,
                trace.provider_start_ms,
                trace.first_token_ms,
                trace.provider_latency_ms,
                trace.total_ms,
                trace.error,
            ),
        )
        await self._conn.commit()

    async def recent(self, kind: str, limit: int = 200) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            """
            SELECT * FROM request_traces
            WHERE kind = ?
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (kind, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def percentiles(self, kind: str, limit: int = 200) -> dict[str, Any]:
        rows = await self.recent(kind, limit=limit)
        totals = sorted(r["total_ms"] for r in rows if r["total_ms"] is not None)
        errors = sum(1 for r in rows if r["error"])
        return {
            "kind": kind,
            "sample_size": len(totals),
            "error_count": errors,
            "p50_ms": _percentile(totals, 50),
            "p90_ms": _percentile(totals, 90),
            "p95_ms": _percentile(totals, 95),
            "max_ms": totals[-1] if totals else None,
        }


def _percentile(sorted_values: list[float], pct: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100) * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    frac = rank - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * frac
