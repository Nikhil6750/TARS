"""ChartWatchService — TARS Alexa-Speed Phase C2 (Python side).

Receives frame pings from the native BackgroundChartWatcher
(`apps/web/src-tauri/src/chart_watcher.rs`, which does cheap local capture
+ perceptual-hash diffing and only pings this backend when it thinks
something is worth a look) and decides whether that ping is actually worth
spending a real Claude Code CLI vision call on. The Rust side's job is "is
it worth pinging the backend"; this service's job is "is it worth spending
real money/latency on a vision call" -- two different, deliberately
separate throttles:

- Rust enforces a floor between HTTP pushes (see chart_watcher.rs's
  MIN_PUSH_COOLDOWN) so a hash value oscillating at the diff threshold
  can't spam this endpoint.
- This service enforces a *longer* per-window cooldown
  (`min_vision_cooldown_seconds`) between actual vision calls, since each
  one costs real CLI usage and ~15-31s of real latency (see this repo's
  own Phase A baseline evidence) -- independent of how often Rust pings.
- Once a window's chart_window_id has *any* prior analyzed identity, this
  service also skips the vision call if that state is still fresh per
  HotChartState's own timeframe-adaptive freshness rules
  (`HotChartState.usable_for`) -- there is no point spending a vision call
  to refresh something that isn't stale yet, even if Rust detected some
  visual change (e.g. a tooltip, a hover highlight, a blinking cursor).

Reuses `ChartAnalysisService.analyze()` (the same call the user-triggered
"analyze the chart" path uses -- same system prompt, same epistemic
discipline, same disclaimer) rather than a second, parallel vision-call
path. Reuses `HotChartStateStore` for persistence. Never talks to
quant_brain, never upgrades "no validated trade" into a signal -- nothing
here changes what `ChartAnalysisResult` is allowed to claim.
"""
from __future__ import annotations

import io
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from PIL import Image

from assistant.chart_analysis import ChartAnalysisService
from assistant.hot_chart_state import ChartIdentity, HotChartState
from assistant.hot_chart_state_store import HotChartStateStore
from assistant.perceptual_hash import average_hash_hex, is_same_chart_content

DEFAULT_MIN_VISION_COOLDOWN_SECONDS = 20.0


@dataclass
class ChartWatchOutcome:
    action: str  # "refreshed" | "skipped_cooldown" | "skipped_fresh" | "error"
    chart_window_id: str
    identity: ChartIdentity | None = None
    error: str | None = None


class ChartWatchService:
    def __init__(
        self,
        chart_analysis_service: ChartAnalysisService,
        hot_state_store: HotChartStateStore,
        *,
        min_vision_cooldown_seconds: float = DEFAULT_MIN_VISION_COOLDOWN_SECONDS,
    ):
        self._chart_analysis = chart_analysis_service
        self._store = hot_state_store
        self._min_cooldown = min_vision_cooldown_seconds
        # In-process only, per chart_window_id -- deliberately not
        # persisted: this is a rate-limit clock, not state that needs to
        # survive a backend restart (a restart naturally resets the
        # throttle, which is safe -- worst case is one extra vision call
        # right after startup, never a correctness issue).
        self._last_vision_call_at: dict[str, float] = {}

    async def handle_frame(
        self,
        *,
        chart_window_id: str,
        image_bytes: bytes,
        image_format: str,
        trigger_reason: str,
    ) -> ChartWatchOutcome:
        now = time.monotonic()
        last_call = self._last_vision_call_at.get(chart_window_id)
        if last_call is not None and (now - last_call) < self._min_cooldown:
            return ChartWatchOutcome(action="skipped_cooldown", chart_window_id=chart_window_id)

        try:
            current_hash = average_hash_hex(Image.open(io.BytesIO(image_bytes)))
        except Exception:
            current_hash = None  # undecodable -- let analyze() raise its own honest error below

        existing = await self._store.get_latest_for_window(chart_window_id)
        if (
            existing is not None
            and existing.usable_for(existing.identity)
            and current_hash is not None
            and is_same_chart_content(current_hash, existing.screenshot_hash)
        ):
            # Time-fresh AND still visually the same chart -- a real
            # symbol/timeframe switch changes the hash enough to fail this
            # check even if it happens well inside the freshness window
            # (Part 19/26: age alone must never be the only cache-
            # correctness signal).
            return ChartWatchOutcome(
                action="skipped_fresh", chart_window_id=chart_window_id, identity=existing.identity
            )

        self._last_vision_call_at[chart_window_id] = now
        observed_at = datetime.now(UTC).isoformat()

        try:
            result = await self._chart_analysis.analyze(
                image_bytes=image_bytes,
                image_format=image_format,
                conversation_id=f"chart-watch-{chart_window_id}",
                goal_text="Analyze this chart.",
            )
        except Exception as exc:  # noqa: BLE001 -- surfaced honestly, never silently dropped
            return ChartWatchOutcome(action="error", chart_window_id=chart_window_id, error=str(exc))

        identity = ChartIdentity(
            chart_window_id=chart_window_id, symbol=result.instrument, timeframe=result.timeframe
        )
        state = HotChartState(
            identity=identity,
            analysis=result,
            screenshot_hash=current_hash or "",
            source="vision",
            observed_at=observed_at,
            analyzed_at=datetime.now(UTC).isoformat(),
        )
        await self._store.upsert(state)
        return ChartWatchOutcome(action="refreshed", chart_window_id=chart_window_id, identity=identity)
