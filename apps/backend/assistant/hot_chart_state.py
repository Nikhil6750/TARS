"""HotChartState — TARS Alexa-Speed Phase B.

A cached, timestamped snapshot of the last vision-grounded chart read for a
specific chart window/symbol/timeframe identity, so a "analyze the chart"
request can be answered from state that is already there instead of always
waiting on a fresh Claude Code CLI vision call (which this repo's own prior
benchmarking — see commits 8ce9ea1/9159069 — measured at ~15-31s,
essentially all real model inference time, not overhead this backend
controls).

Deliberately reuses `assistant.chart_analysis.ChartAnalysisResult` as the
analysis payload rather than duplicating its fields — a HotChartState is
that same result plus identity + freshness metadata, nothing more. This is
also why it inherits that result's epistemic discipline for free: no
confidence percentage, no guaranteed prediction, disclaimer always present.

HotChartState is OBSERVATION + INTERPRETATION + HYPOTHESIS. It is never
validated strategy state -- see `trading.provider.StrategyProvider`/
`agent_runtime.quant_boundary.QuantBrainBoundary` for the only source of a
validated trade signal (today: none are configured; every StrategyProvider
in this codebase returns NOT_CONFIGURED). Nothing in this module upgrades
"no validated trade" into a signal, and nothing here talks to quant_brain.

Freshness thresholds are timeframe-adaptive per the task's own guidance
("for 15m chart structure, moderate state age may be acceptable; for
faster timeframes, freshness threshold should be shorter") and are
deliberately conservative and documented, not silently invented:

- Sub-5-minute charts (1m/3m/5m): HOT <=20s, WARM <=45s. Price action on
  these timeframes can materially change within tens of seconds.
- 15m-30m charts: HOT <=60s, WARM <=180s (3min). A quarter-hour candle
  isn't meaningfully stale after a minute of background silence.
- 1H and slower (1H/4H/1D/1W): HOT <=180s (3min), WARM <=600s (10min).
  Structure on these timeframes moves slowly enough that a few minutes of
  staleness is still an honest "current" read.
- Unrecognized/unparseable timeframe text: treated with the sub-5-minute
  (most conservative) thresholds, since assuming a slower cadence is safe
  when the timeframe itself is unknown would risk presenting stale state
  as current.

This module is purely additive in Phase B: nothing in the live request
path constructs, stores, or reads a HotChartState yet. Phase C wires the
background watcher that populates it; Phase D wires the fast read path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from assistant.chart_analysis import ChartAnalysisResult


class Freshness(str, Enum):
    HOT = "hot"
    WARM = "warm"
    STALE = "stale"
    MISSING = "missing"


@dataclass(frozen=True)
class ChartIdentity:
    """What a cached analysis must match to be reused for a new request.

    `chart_window_id` is a stable handle to the physical window the
    capture came from (e.g. the native HWND as a string) -- distinct from
    `symbol`/`timeframe`, which are the model's own read of the chart's
    on-screen labels and can change without the window itself changing
    (e.g. the user switches symbols in the same TradingView window).
    A lookup must match on all three; any mismatch means "cannot reuse,
    full stop" (Part 19's cache-correctness rule), never a partial/fuzzy
    match.
    """

    chart_window_id: str
    symbol: str | None
    timeframe: str | None

    def matches(self, other: ChartIdentity) -> bool:
        return (
            self.chart_window_id == other.chart_window_id
            and self.symbol == other.symbol
            and self.timeframe == other.timeframe
        )


@dataclass
class HotChartState:
    identity: ChartIdentity
    analysis: ChartAnalysisResult
    screenshot_hash: str
    # "vision" today (a Claude Code CLI vision call) -- reserved for a
    # future "structured" value if/when a real non-vision market-data
    # source ever exists (none does anywhere in this repo today -- see
    # the Phase A recon that grounds this whole effort).
    source: str
    observed_at: str  # ISO 8601 UTC -- when the frame was captured
    analyzed_at: str  # ISO 8601 UTC -- when the vision analysis completed
    version: int = 1

    def age_ms(self, *, now: datetime | None = None) -> float:
        reference = now or datetime.now(UTC)
        analyzed = datetime.fromisoformat(self.analyzed_at)
        return max(0.0, (reference - analyzed).total_seconds() * 1000)

    def freshness(self, *, now: datetime | None = None) -> Freshness:
        age = self.age_ms(now=now)
        hot_ms, warm_ms = _freshness_thresholds_ms(self.identity.timeframe)
        if age <= hot_ms:
            return Freshness.HOT
        if age <= warm_ms:
            return Freshness.WARM
        return Freshness.STALE

    def usable_for(self, identity: ChartIdentity, *, now: datetime | None = None) -> bool:
        """True only if this state matches the requested identity exactly
        AND is not STALE. A caller with a MISSING/STALE/mismatched result
        must fall through to a fresh (cold) analysis -- never present
        outdated or wrong-symbol/timeframe state as current."""
        if not self.identity.matches(identity):
            return False
        return self.freshness(now=now) in (Freshness.HOT, Freshness.WARM)


# (hot_ms, warm_ms) by timeframe bucket -- see module docstring for the
# rationale behind each tier.
_SUB_5M_THRESHOLDS = (20_000.0, 45_000.0)
_15M_30M_THRESHOLDS = (60_000.0, 180_000.0)
_1H_PLUS_THRESHOLDS = (180_000.0, 600_000.0)

_TIMEFRAME_PATTERN = re.compile(
    r"^\s*(\d+)\s*([mMhHdDwW])\s*$"
)


def _parse_timeframe_minutes(raw: str | None) -> int | None:
    """Best-effort parse of free-text timeframe labels the vision model
    returns (e.g. "15M", "4H", "1D", "5m") into minutes. Returns None for
    anything unrecognized -- callers must treat that as "unknown", not
    guess a value."""
    if not raw:
        return None
    match = _TIMEFRAME_PATTERN.match(raw)
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2).lower()
    multiplier = {"m": 1, "h": 60, "d": 60 * 24, "w": 60 * 24 * 7}[unit]
    return value * multiplier


def _freshness_thresholds_ms(timeframe: str | None) -> tuple[float, float]:
    minutes = _parse_timeframe_minutes(timeframe)
    if minutes is None:
        return _SUB_5M_THRESHOLDS  # unknown timeframe -> most conservative
    if minutes < 15:
        return _SUB_5M_THRESHOLDS
    if minutes <= 30:
        return _15M_30M_THRESHOLDS
    return _1H_PLUS_THRESHOLDS
