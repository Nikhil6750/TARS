"""Strict data-quality engine. Any BLOCK issue blocks an experiment from running."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from .schema import CANONICAL_COLUMNS, PRICE_COLUMNS, TIMEFRAME_MINUTES

BLOCK, WARN, INFO = "BLOCK", "WARN", "INFO"


@dataclass
class QualityIssue:
    code: str
    severity: str
    count: int
    detail: str


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

    def add(code: str, sev: str, count: int, detail: str) -> None:
        if count:
            rep.issues.append(QualityIssue(code, sev, int(count), detail))

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

    # Calendar structure: weekends/closures, missing bars, large gaps (only meaningful for sorted unique data).
    closed = _market_closed(ts)
    add("BARS_DURING_MARKET_CLOSURE", WARN, closed.sum(), "bars stamped inside the weekly closure window")
    s = ts.drop_duplicates().sort_values().reset_index(drop=True)
    gaps = s.diff().dropna()
    over = gaps[gaps > step]
    if len(over):
        starts = s.iloc[over.index - 1].reset_index(drop=True)
        ends = s.iloc[over.index].reset_index(drop=True)
        gap_td = over.reset_index(drop=True)
        weekend = np.array([bool(_market_closed(pd.Series([a + step])).iloc[0]) or
                            (b - a) >= pd.Timedelta(hours=40) for a, b in zip(starts, ends)])
        missing_bars = int(((gap_td // step) - 1)[~weekend].sum())
        expected_in_session = max(int((~_market_closed(s)).sum()) + missing_bars, 1)
        frac = missing_bars / expected_in_session
        add("MISSING_BARS", BLOCK if frac > max_missing_fraction else WARN, missing_bars,
            f"{frac:.2%} of expected in-session bars (limit {max_missing_fraction:.0%})")
        large = (~weekend) & (gap_td > pd.Timedelta(minutes=max_gap_minutes))
        add("LARGE_GAPS", BLOCK, large.sum(), f"gaps longer than {max_gap_minutes:.0f} minutes outside the weekly closure")
        # provider-style discontinuity: non-weekend close->open jump far beyond typical bar moves
    ret = np.log(bars["bid_close"]).diff().abs()
    if ret.notna().sum() > 50:
        sd = float(ret.std())
        add("PRICE_DISCONTINUITY", WARN, (ret > 25 * sd).sum() if sd > 0 else 0, "bar-to-bar jump > 25 sigma")
    return rep
