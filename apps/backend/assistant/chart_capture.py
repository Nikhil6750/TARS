"""Shared "fresh capture -> verify it's a chart -> vision analyze" primitive.

Extracted from AssistantTurnController._chart_analysis (the single-shot
"analyze this chart" path, see turn_controller.py) so the Universal Market
Explainer's multi-timeframe collection loop (trading/market_explainer.py)
can reuse the exact same capture/verify/decode logic instead of a second
implementation -- there is exactly one way this backend turns "what's on
screen" into a chart vision read, and exactly one place that knows
capture_active_window returns a `data:` URI, exactly one chart-identity
check, exactly one error-shaping convention.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import re
from dataclasses import dataclass

from app.action_contracts import ActionRequest, ActionSource, ActionStatus
from assistant.chart_analysis import ChartAnalysisError, ChartAnalysisResult, ChartAnalysisService
from assistant.errors import AssistantProviderError

# What counts as "a supported chart" for the fresh capture's identity
# verification -- matched against the freshly captured window's executable
# and title. TradingView only today (same scope as
# skills/tradingview_control.py and chart_watcher.rs's find_chart_window());
# a stale/wrong window (e.g. the user switched to Chrome) must never be
# sent to the vision model just because it was once the tracked chart.
CHART_WINDOW_RE = re.compile(r"tradingview", re.IGNORECASE)

_OUTCOME_STATUSES = (
    "ANALYZED", "NOT_A_CHART", "CAPTURE_FAILED", "SECURE_DESKTOP",
    "EMPTY_IMAGE", "CORRUPTED", "ANALYSIS_ERROR",
)


@dataclass
class ChartCaptureOutcome:
    """`status` is one of _OUTCOME_STATUSES; only "ANALYZED" carries a
    `result`. `message` is always a truthful, user-facing sentence --
    for ANALYZED it is empty (callers use `result.formatted_tars_text()`
    instead)."""

    status: str
    message: str
    result: ChartAnalysisResult | None = None
    executable: str = ""
    window_title: str = ""

    @property
    def analyzed(self) -> bool:
        return self.status == "ANALYZED"


async def capture_and_analyze_chart(
    action_runtime,
    chart_analysis_service: ChartAnalysisService,
    *,
    goal_text: str = "Analyze this chart.",
    conversation_id: str = "chart-capture",
    capture_timeout: float = 20.0,
) -> ChartCaptureOutcome:
    """Fresh, synchronous, bounded capture -> identity check -> vision
    analyze. Never reads a cache, never waits on a background watcher --
    every call physically captures the screen again right now."""
    capture_request = ActionRequest(
        skill="windows_app",
        action="capture_active_window",
        arguments={"include_image_data": True},
        source=ActionSource.deterministic,
    )
    try:
        capture_result = await asyncio.wait_for(action_runtime.submit(capture_request), capture_timeout)
    except TimeoutError:
        return ChartCaptureOutcome("CAPTURE_FAILED", "I couldn't capture the screen in time to analyze it.")
    except Exception:
        return ChartCaptureOutcome("CAPTURE_FAILED", "I couldn't capture the screen to analyze it.")

    if capture_result.status is not ActionStatus.SUCCEEDED:
        return ChartCaptureOutcome(
            "CAPTURE_FAILED", capture_result.summary or "I couldn't capture the screen to analyze it."
        )

    data = capture_result.data or {}
    if data.get("is_secure_desktop"):
        return ChartCaptureOutcome(
            "SECURE_DESKTOP", "I can't capture the screen right now (a secure system dialog is active)."
        )
    if data.get("error"):
        return ChartCaptureOutcome("CAPTURE_FAILED", f"I couldn't capture the screen to analyze it: {data['error']}")

    executable = str(data.get("executable") or "")
    window_title = str(data.get("window_title") or "")
    if not CHART_WINDOW_RE.search(executable) and not CHART_WINDOW_RE.search(window_title):
        shown = window_title or executable or "nothing identifiable"
        return ChartCaptureOutcome(
            "NOT_A_CHART",
            f"What's in front right now doesn't look like a supported chart (I see '{shown}'). "
            "Bring the chart to the front and ask again.",
            executable=executable, window_title=window_title,
        )

    image_data = data.get("image_data_base64")
    if not isinstance(image_data, str) or not image_data.strip():
        return ChartCaptureOutcome(
            "EMPTY_IMAGE", "I couldn't capture an image of the chart to analyze.",
            executable=executable, window_title=window_title,
        )
    # capture_active_window returns a data: URI (see lib.rs), not raw
    # base64 -- same encoding the REST analyze-chart endpoint strips in
    # app/routers/assistant.py.
    _, _, encoded_image = image_data.partition(",") if image_data.startswith("data:") else ("", "", image_data)
    try:
        image_bytes = base64.b64decode(encoded_image, validate=True)
    except (binascii.Error, ValueError):
        return ChartCaptureOutcome(
            "CORRUPTED", "The chart capture came back corrupted; please try again.",
            executable=executable, window_title=window_title,
        )
    if not image_bytes:
        return ChartCaptureOutcome(
            "EMPTY_IMAGE", "The chart capture came back empty; please try again.",
            executable=executable, window_title=window_title,
        )

    image_format = str(data.get("image_format") or "image/png")
    active_context_text = (
        f"active application: {executable}; window title: {window_title}" if executable or window_title else ""
    )
    try:
        result = await chart_analysis_service.analyze(
            image_bytes=image_bytes,
            image_format=image_format,
            conversation_id=conversation_id,
            active_context_text=active_context_text,
            goal_text=goal_text,
        )
    except ChartAnalysisError as exc:
        return ChartCaptureOutcome(
            "ANALYSIS_ERROR", f"I couldn't analyze that capture: {exc}",
            executable=executable, window_title=window_title,
        )
    except AssistantProviderError as exc:
        return ChartCaptureOutcome(
            "ANALYSIS_ERROR", f"The chart analysis failed: {exc}",
            executable=executable, window_title=window_title,
        )

    return ChartCaptureOutcome("ANALYZED", "", result=result, executable=executable, window_title=window_title)
