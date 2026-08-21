"""FAST_CHART_ANALYSIS path — TARS Alexa-Speed Phase D.

Decides whether "analyze the chart" can be answered immediately from
`HotChartState` instead of waiting on a fresh Claude Code CLI vision call
(this repo's own Phase A baseline: ~15-31s dominated by real model
inference). Never calls the provider itself -- `try_fast_response()`
either returns an SSE-shaped payload built entirely from already-stored,
already-analyzed state, or returns `None`, in which case the caller must
fall through to the existing cold `ChartAnalysisService.analyze_stream()`
path unchanged (Part 20: never fabricate a fast answer when there isn't
one to honestly give).

Deliberately conservative for this first cut: only `Freshness.HOT` state
is served fast (not `WARM`, even though `HotChartState.usable_for()` would
allow it) -- serving `WARM` state honestly needs an explicit "as of Ns
ago" caveat appended to the spoken/text response, which is a real design
piece of its own (`ChartAnalysisResult.formatted_tars_text()`/
`.speech_text()` have no such hook today) and is left as a documented
follow-up rather than half-built here. This is a strictly safer default,
not a shortcut -- a HOT-only fast path can only ever get faster (once
WARM is added) or stay the same, never regress correctness.

Also enforces the Part 19/26 cache-correctness rule the age-only
freshness check cannot: `chart_window_id` alone does not encode symbol/
timeframe, so a time-fresh row for a *different* symbol (the user just
switched charts) must not be served. `is_same_chart_content` (a cheap
perceptual-hash comparison against the *current* request's own capture)
is what actually enforces this -- see assistant/perceptual_hash.py.
"""
from __future__ import annotations

import io
import time
from dataclasses import dataclass
from typing import Any

from PIL import Image

from assistant.hot_chart_state import Freshness
from assistant.hot_chart_state_store import HotChartStateStore
from assistant.perceptual_hash import average_hash_hex, is_same_chart_content


@dataclass
class FastResponse:
    result: dict[str, Any]
    timing: dict[str, Any]


async def try_fast_response(
    *,
    window_id: str | None,
    image_bytes: bytes,
    hot_state_store: HotChartStateStore,
    t0: float,
) -> FastResponse | None:
    if not window_id:
        return None

    existing = await hot_state_store.get_latest_for_window(window_id)
    if existing is None or existing.freshness() != Freshness.HOT:
        return None

    try:
        current_hash = average_hash_hex(Image.open(io.BytesIO(image_bytes)))
    except Exception:
        return None
    if not is_same_chart_content(current_hash, existing.screenshot_hash):
        return None

    elapsed_ms = round((time.monotonic() - t0) * 1000)
    return FastResponse(
        result=existing.analysis.to_dict(),
        timing={
            "claude_start_ms": 0,
            "first_token_ms": elapsed_ms,
            "complete_ms": elapsed_ms,
            "warm_path": True,
            "state_age_ms": round(existing.age_ms()),
        },
    )
