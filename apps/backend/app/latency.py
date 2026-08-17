"""Latency instrumentation for the TARS voice/text pipeline: wake -> STT ->
reasoning -> first-token -> TTS, per the TARS core § Performance goal
("measure wake→STT→reasoning→first-token→TTS latency"). A small,
dependency-free checkpoint tracker plus an OpenTelemetry span emitter — no
new observability stack, reusing `app.observability.get_tracer()` exactly
as every other traced call in this backend does (ARCHITECTURE.md §
Observability: latency is one of the things TARS must be able to log, never
secrets).

Usage: create one `LatencyTracker` per request/utterance, call `.mark(name)`
at each stage boundary as it happens, then `.emit_span()` once at the end.
Stage names are caller-defined strings (not an enum) so this utility works
for a text-only turn (e.g. just "reasoning_start"/"first_token"/
"reasoning_end") as well as a full voice turn (adding "wake"/"stt_start"/
"stt_end"/"tts_start"/"tts_end") without forcing every caller through every
stage.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.observability import get_tracer

tracer = get_tracer()


@dataclass
class LatencyTracker:
    _marks: dict[str, float] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)
    _started_at: float = field(default_factory=time.monotonic)

    def mark(self, name: str) -> None:
        if name not in self._marks:
            self._order.append(name)
        self._marks[name] = time.monotonic()

    def elapsed_since_start(self, name: str) -> float | None:
        if name not in self._marks:
            return None
        return self._marks[name] - self._started_at

    def deltas(self) -> dict[str, float]:
        """Milliseconds between each consecutive pair of marks, in the order
        they were first recorded, plus a `total_ms` from the first mark to
        the last."""
        result: dict[str, float] = {}
        for previous, current in zip(self._order, self._order[1:], strict=False):
            result[f"{previous}_to_{current}_ms"] = (
                self._marks[current] - self._marks[previous]
            ) * 1000
        if len(self._order) >= 2:
            result["total_ms"] = (
                self._marks[self._order[-1]] - self._marks[self._order[0]]
            ) * 1000
        return result

    def emit_span(self, span_name: str = "latency.turn") -> dict[str, float]:
        deltas = self.deltas()
        with tracer.start_as_current_span(span_name) as span:
            for key, value_ms in deltas.items():
                span.set_attribute(f"latency.{key}", round(value_ms, 2))
        return deltas
