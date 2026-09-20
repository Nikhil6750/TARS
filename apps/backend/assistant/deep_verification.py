"""Asynchronous deep verification — TARS Alexa-Speed Phase E.

After the fast path (Phase D) serves a response from `HotChartState`, this
runs a fresh vision call in the background (after the response has already
gone out, via FastAPI `BackgroundTasks` -- never blocks what the user
already received) and compares it against what was just served. Only a
*material* difference -- bias, setup classification, action, invalidation,
or the visible key levels changed -- updates the stored state for the next
request; cosmetic wording differences in free-text fields never do (Part
7: "do not interrupt the user with trivial wording differences").

Never upgrades "no validated trade" into a signal and never talks to
quant_brain -- this is the exact same `ChartAnalysisService.analyze()`
call every other vision path in this codebase uses, with the exact same
epistemic discipline. There is currently no push channel to actively
notify a connected client of a correction (Phase C deliberately deferred
building that with no consumer yet); this module's effect today is
narrower but still real: the *next* fast-path request sees the corrected
state instead of a materially wrong cached read.
"""
from __future__ import annotations

import io
import logging
from dataclasses import fields
from datetime import UTC, datetime

from PIL import Image

from assistant.chart_analysis import ChartAnalysisResult, ChartAnalysisService
from assistant.hot_chart_state import ChartIdentity, HotChartState
from assistant.hot_chart_state_store import HotChartStateStore
from assistant.perceptual_hash import average_hash_hex

logger = logging.getLogger("tars.deep_verification")

_RESULT_FIELD_NAMES = {f.name for f in fields(ChartAnalysisResult)}


def _result_from_served_dict(served_result: dict) -> ChartAnalysisResult:
    """`served_result` is `ChartAnalysisResult.to_dict()`'s output, which
    adds `speech_text`/`formatted_tars_text` on top of the dataclass's own
    fields (see chart_analysis.py) -- those two are not constructor
    arguments, so they must be dropped before reconstructing."""
    return ChartAnalysisResult(**{k: v for k, v in served_result.items() if k in _RESULT_FIELD_NAMES})


def materially_different(old: ChartAnalysisResult, new: ChartAnalysisResult) -> bool:
    """True if `new` changes something a user would actually care about
    hearing again -- structure/level/invalidation/risk/scenario, per Part
    7 -- not wording. If either side didn't parse into the structured
    shape, the two free-text reads can't be compared field-by-field, so
    this conservatively reports "different" rather than silently treating
    two unrelated paragraphs as equivalent."""
    if not old.structured or not new.structured:
        return old.raw_text.strip() != new.raw_text.strip()

    return (
        old.bias != new.bias
        or old.setup != new.setup
        or old.action != new.action
        or old.invalidation != new.invalidation
        or set(old.key_levels) != set(new.key_levels)
        or old.supply_zone != new.supply_zone
        or old.demand_zone != new.demand_zone
    )


async def run_deep_verification(
    *,
    window_id: str,
    image_bytes: bytes,
    image_format: str,
    chart_analysis_service: ChartAnalysisService,
    hot_state_store: HotChartStateStore,
    served_result: dict,
) -> None:
    """Fire-and-forget background task (see FastAPI `BackgroundTasks` at
    the call site) -- must never raise into the caller; any failure is
    logged and swallowed, since by the time this runs the user already has
    their (fast, honest) answer and nothing here can un-send it."""
    try:
        served = _result_from_served_dict(served_result)
        fresh = await chart_analysis_service.analyze(
            image_bytes=image_bytes,
            image_format=image_format,
            conversation_id=f"deep-verify-{window_id}",
            goal_text="Analyze this chart.",
        )

        if not materially_different(served, fresh):
            return

        identity = ChartIdentity(chart_window_id=window_id, symbol=fresh.instrument, timeframe=fresh.timeframe)
        state = HotChartState(
            identity=identity,
            analysis=fresh,
            screenshot_hash=average_hash_hex(Image.open(io.BytesIO(image_bytes))),
            source="vision",
            observed_at=datetime.now(UTC).isoformat(),
            analyzed_at=datetime.now(UTC).isoformat(),
        )
        await hot_state_store.upsert(state)
        logger.info(
            "deep verification found a material difference for window=%s; state updated for the next request",
            window_id,
        )
    except Exception:  # noqa: BLE001 -- background task, must never propagate
        logger.exception("deep verification failed for window=%s", window_id)
