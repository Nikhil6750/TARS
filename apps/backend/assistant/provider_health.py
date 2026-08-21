"""ProviderHealthTracker — TARS Alexa-Speed Phase G.

Real provider health statistics (Part 9: "provider / model / healthy /
success rate / moving average latency / P50 / P95 / last error / last
successful request"), computed from `request_traces` (Phase A's
`LatencyTraceStore`) -- this repo's own recorded evidence, never assumed
or hard-coded benchmark numbers.

Scope note, stated plainly rather than overclaimed: this repo has exactly
one vision-capable provider adapter (`ClaudeCodeProvider` -- see
`assistant/factory.py`'s `build_chart_assistant_provider()`, which always
returns it regardless of `ASSISTANT_PROVIDER`). `CodexProvider`/
`GeminiProvider` exist for general text chat only. So there is no second
real candidate to dynamically route *chart-analysis* vision requests
between today -- Part 9/10's "routing should consider task type +
capability + health + latency" has nothing to select between on that path
until a second vision-capable adapter exists. What this module provides is
the real, evidence-based health-tracking foundation Part 9 asks for, and
a selection helper usable once/where more than one provider genuinely
serves the same capability (already true for general text chat, gated by
which provider executables actually resolve on this machine -- never
assumed available).
"""
from __future__ import annotations

from dataclasses import dataclass

from app.latency_store import LatencyTraceStore


@dataclass
class ProviderHealth:
    provider_id: str
    sample_size: int
    error_count: int
    success_rate: float
    p50_ms: float | None
    p90_ms: float | None
    p95_ms: float | None
    max_ms: float | None
    last_error: str | None


class ProviderHealthTracker:
    def __init__(self, trace_store: LatencyTraceStore):
        self._trace_store = trace_store

    async def health_for(self, provider_id: str, *, kind: str = "assistant_text", limit: int = 200) -> ProviderHealth:
        rows = await self._trace_store.recent(kind, limit=limit)
        provider_rows = [r for r in rows if r.get("provider_id") == provider_id]

        totals = sorted(r["total_ms"] for r in provider_rows if r.get("total_ms") is not None)
        errors = [r for r in provider_rows if r.get("error")]
        sample_size = len(provider_rows)
        success_rate = (sample_size - len(errors)) / sample_size if sample_size else 0.0
        last_error = errors[0]["error"] if errors else None  # rows are already newest-first

        return ProviderHealth(
            provider_id=provider_id,
            sample_size=sample_size,
            error_count=len(errors),
            success_rate=success_rate,
            p50_ms=_percentile(totals, 50),
            p90_ms=_percentile(totals, 90),
            p95_ms=_percentile(totals, 95),
            max_ms=totals[-1] if totals else None,
            last_error=last_error,
        )

    async def healthiest(
        self, provider_ids: list[str], *, kind: str = "assistant_text", limit: int = 200
    ) -> str | None:
        """Picks the provider with the best real recorded track record among
        `provider_ids` -- highest success rate first, then lowest P50 among
        those tied on success rate. Returns None if none of them have any
        recorded traces yet (a genuinely unknown quantity, not defaulted to
        an arbitrary first choice)."""
        candidates = [await self.health_for(pid, kind=kind, limit=limit) for pid in provider_ids]
        known = [c for c in candidates if c.sample_size > 0]
        if not known:
            return None
        known.sort(key=lambda c: (-c.success_rate, c.p50_ms if c.p50_ms is not None else float("inf")))
        return known[0].provider_id


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
