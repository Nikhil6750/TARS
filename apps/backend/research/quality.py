"""Strict data-quality engine. Any BLOCK issue blocks an experiment from running."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from .instruments import get_meta, has_meta
from .schema import CANONICAL_COLUMNS, PRICE_COLUMNS, TIMEFRAME_MINUTES
from .sessions import is_daily_break, is_known_holiday, is_market_closed

BLOCK, WARN, INFO = "BLOCK", "WARN", "INFO"


@dataclass
class QualityIssue:
    code: str
    severity: str
    count: int
    detail: str
    category: str = "WARNING"  # EXPECTED_GAP | MARKET_CLOSED | PROVIDER_GAP | INVALID | WARNING


@dataclass
class DataQualityReport:
    rows: int
    instrument: str
    first_ts: str | None
    last_ts: str | None
    issues: list[QualityIssue] = field(default_factory=list)

    @property
    def blocks_experiment(self) -> bool:
        return any(i.severity == BLOCK for i in self.issues)

    @property
    def passed(self) -> bool:
        return not self.blocks_experiment

    def codes(self, severity: str | None = None) -> set[str]:
        return {i.code for i in self.issues if severity is None or i.severity == severity}

    def to_dict(self) -> dict:
        return {"rows": self.rows, "instrument": self.instrument, "first_ts": self.first_ts,
                "last_ts": self.last_ts, "passed": self.passed, "issues": [asdict(i) for i in self.issues]}


def _market_closed(ts: pd.Series) -> pd.Series:
    """FX/metals weekly closure (UTC): Fri 22:00 -> Sun 22:00 (approximate; DST shifts it by an hour)."""
    dow, hour = ts.dt.dayofweek, ts.dt.hour
    return (dow == 5) | ((dow == 4) & (hour >= 22)) | ((dow == 6) & (hour < 22))


def validate_bars(bars: pd.DataFrame, *, timeframe: str | None = None, max_missing_fraction: float = 0.02,
                  max_gap_minutes: float = 240.0, spread_outlier_mult: float = 8.0) -> DataQualityReport:
    inst = str(bars["instrument"].iloc[0]) if len(bars) and "instrument" in bars else "?"
    rep = DataQualityReport(rows=len(bars), instrument=inst, first_ts=None, last_ts=None)

    def add(code: str, sev: str, count: int, detail: str, category: str | None = None) -> None:
        if count:
            cat = category or ("INVALID" if sev == BLOCK else "WARNING")
            rep.issues.append(QualityIssue(code, sev, int(count), detail, cat))

    missing_cols = [c for c in CANONICAL_COLUMNS if c not in bars.columns]
    if missing_cols:
        add("SCHEMA_MISSING_COLUMNS", BLOCK, len(missing_cols), ",".join(missing_cols))
        return rep
    if len(bars) == 0:
        add("EMPTY_DATASET", BLOCK, 1, "no rows")
        return rep

    ts = bars["timestamp_utc"]
    if not isinstance(ts.dtype, pd.DatetimeTZDtype) or str(ts.dt.tz) != "UTC":
        add("TIMEZONE_NOT_UTC", BLOCK, len(bars), f"timestamp dtype {ts.dtype}")
        return rep
    rep.first_ts, rep.last_ts = str(ts.iloc[0]), str(ts.iloc[-1])
    tf = timeframe or str(bars["source_timeframe"].iloc[0])
    step = pd.Timedelta(minutes=TIMEFRAME_MINUTES.get(tf, 1))

    add("MIXED_INSTRUMENTS", BLOCK, bars["instrument"].nunique() - 1, "dataset must hold one instrument")
    add("PROVIDER_DISCONTINUITY", BLOCK, bars["provider"].nunique() - 1,
        "multiple providers inside one series: " + ",".join(sorted(map(str, bars["provider"].unique()))))

    add("DUPLICATE_TIMESTAMPS", BLOCK, ts.duplicated().sum(), "repeated timestamp_utc")
    add("OUT_OF_ORDER", BLOCK, (ts.diff() < pd.Timedelta(0)).sum(), "timestamps not monotonic increasing")

    px = bars[list(PRICE_COLUMNS)]
    add("NULL_PRICES", BLOCK, px.isna().any(axis=1).sum(), "NaN/None price")
    add("NONPOSITIVE_PRICES", BLOCK, (px <= 0).any(axis=1).sum(), "zero or negative price")
    for side in ("bid", "ask"):
        o, h, l, c = (bars[f"{side}_{f}"] for f in ("open", "high", "low", "close"))
        bad = (h < np.maximum(o, c)) | (l > np.minimum(o, c)) | (h < l)
        add(f"INVALID_OHLC_{side.upper()}", BLOCK, bad.sum(), f"{side}: high<max(o,c), low>min(o,c) or high<low")

    crossed = (bars["ask_open"] < bars["bid_open"]) | (bars["ask_high"] < bars["bid_high"]) | \
              (bars["ask_low"] < bars["bid_low"]) | (bars["ask_close"] < bars["bid_close"])
    add("BID_ASK_CROSSED", BLOCK, crossed.sum(), "ask below bid")
    spread = bars["ask_close"] - bars["bid_close"]
    add("NEGATIVE_SPREAD", BLOCK, (bars["spread"] < 0).sum(), "stored spread < 0")
    add("SPREAD_INCONSISTENT", WARN, ((bars["spread"] - spread).abs() > 1e-9 + 1e-6 * bars["ask_close"].abs()).sum(),
        "stored spread != ask_close - bid_close")
    pos = spread[spread > 0]
    if len(pos) >= 30:
        med = float(pos.median())
        add("ABNORMAL_SPREAD", WARN, (spread > med * spread_outlier_mult).sum(),
            f"spread > {spread_outlier_mult}x median ({med:.6g})")
    add("ZERO_SPREAD", INFO, (spread == 0).sum(), "ask_close == bid_close")

    # Calendar structure. Gaps are classified, never repaired: MARKET_CLOSED (weekly closure), EXPECTED_GAP (daily break /
    # known holiday), PROVIDER_GAP (data that should exist but does not).
    meta = get_meta(inst) if has_meta(inst) else None
    closed = is_market_closed(ts, meta) if meta else _market_closed(ts)
    add("BARS_DURING_MARKET_CLOSURE", WARN, closed.sum(), "bars stamped inside the weekly closure window")
    s = ts.drop_duplicates().sort_values().reset_index(drop=True)
    gaps = s.diff().dropna()
    over = gaps[gaps > step]
    n_closed = n_expected = 0
    missing_provider = 0
    large_provider_gaps = 0
    if len(over):
        starts = s.iloc[over.index - 1].reset_index(drop=True)
        ends = s.iloc[over.index].reset_index(drop=True)
        for a_ts, b_ts in zip(starts, ends):
            first_missing, last_missing = a_ts + step, b_ts - step
            probe = pd.Series([first_missing, last_missing])
            if meta:
                weekend = bool(is_market_closed(probe, meta).any())
                brk = bool(is_daily_break(probe, meta).all()) or bool(is_daily_break(pd.Series([first_missing]), meta).iloc[0])
            else:
                weekend, brk = bool(_market_closed(probe).any()), False
            holiday = bool(is_known_holiday(probe).any())
            span = b_ts - a_ts
            if weekend and span <= pd.Timedelta(hours=72):
                n_closed += 1
            elif (brk and (span - step) <= pd.Timedelta(minutes=90)) or (holiday and span <= pd.Timedelta(hours=30)):
                n_expected += 1
            else:
                missing = int(span // step) - 1
                missing_provider += missing
                if span > pd.Timedelta(minutes=max_gap_minutes):
                    large_provider_gaps += 1
    add("GAPS_MARKET_CLOSED", INFO, n_closed, "weekly-closure gaps (expected)", "MARKET_CLOSED")
    add("GAPS_EXPECTED", INFO, n_expected, "daily-break / holiday gaps (expected)", "EXPECTED_GAP")
    in_session_expected = max(int((~closed).sum()) + missing_provider, 1)
    frac = missing_provider / in_session_expected
    add("MISSING_BARS", BLOCK if frac > max_missing_fraction else WARN, missing_provider,
        f"{frac:.2%} of expected in-session bars missing (limit {max_missing_fraction:.0%})", "PROVIDER_GAP")
    add("LARGE_GAPS", BLOCK, large_provider_gaps, f"unexplained gaps longer than {max_gap_minutes:.0f} minutes", "PROVIDER_GAP")
    ret = np.log(bars["bid_close"]).diff().abs()
    if ret.notna().sum() > 50:
        sd = float(ret.std())
        add("PRICE_DISCONTINUITY", WARN, (ret > 25 * sd).sum() if sd > 0 else 0, "bar-to-bar jump > 25 sigma")
    return rep


def gap_summary(rep: DataQualityReport) -> dict:
    out = {"MARKET_CLOSED": 0, "EXPECTED_GAP": 0, "PROVIDER_GAP": 0, "INVALID": 0, "WARNING": 0}
    for i in rep.issues:
        out[i.category] = out.get(i.category, 0) + i.count
    return out
