"""Provider-independent canonical bar schema + deterministic timeframe derivation. All timestamps UTC."""
from __future__ import annotations

import pandas as pd
import pyarrow as pa

PRICE_SIDES = ("bid", "ask")
OHLC = ("open", "high", "low", "close")
PRICE_COLUMNS = tuple(f"{s}_{f}" for s in PRICE_SIDES for f in OHLC)
CANONICAL_COLUMNS = ("timestamp_utc", "instrument", "provider", *PRICE_COLUMNS, "spread", "volume",
                     "source_timeframe", "quality_flags")
TIMEFRAME_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}

ARROW_SCHEMA = pa.schema([
    ("timestamp_utc", pa.timestamp("ns", tz="UTC")), ("instrument", pa.string()), ("provider", pa.string()),
    *[(c, pa.float64()) for c in PRICE_COLUMNS], ("spread", pa.float64()), ("volume", pa.float64()),
    ("source_timeframe", pa.string()), ("quality_flags", pa.string()),
])


def pip_size(instrument: str) -> float:
    from .instruments import pip_size_of
    return pip_size_of(instrument)


def make_bars(df: pd.DataFrame, *, instrument: str, provider: str, source_timeframe: str = "M1") -> pd.DataFrame:
    """Normalise a frame with timestamp_utc + bid_*/ask_* into canonical columns (no value repair)."""
    out = df.copy()
    out["instrument"] = instrument.upper()
    out["provider"] = provider
    out["source_timeframe"] = source_timeframe
    if "spread" not in out:
        out["spread"] = out["ask_close"] - out["bid_close"]
    if "volume" not in out:
        out["volume"] = float("nan")
    if "quality_flags" not in out:
        out["quality_flags"] = ""
    return out[list(CANONICAL_COLUMNS)].reset_index(drop=True)


def _bin_keys(ts: pd.Series, target: str, instrument: str) -> pd.Series:
    """Bin label (UTC) for each timestamp. D1 is anchored to the instrument's trading-day rollover in its own
    timezone (e.g. 17:00 New York, DST-aware); every other timeframe floors on UTC clock boundaries."""
    if target != "D1":
        return ts.dt.floor(f"{TIMEFRAME_MINUTES[target]}min")
    from .instruments import get_meta, has_meta
    if not has_meta(instrument):
        return ts.dt.floor("1D")
    meta = get_meta(instrument)
    local = ts.dt.tz_convert(meta.timezone)
    anchor = pd.Timedelta(hours=meta.daily_anchor_local.hour, minutes=meta.daily_anchor_local.minute)
    shifted = (local.dt.tz_localize(None) - anchor).dt.floor("1D") + anchor  # wall-clock arithmetic -> DST-correct
    return shifted.dt.tz_localize(meta.timezone, ambiguous="NaT", nonexistent="shift_forward").dt.tz_convert("UTC")


def derive_timeframe(bars: pd.DataFrame, target: str, *, drop_partial_tail: bool = False) -> pd.DataFrame:
    """Deterministic resample (left-closed, left-labelled; open=first, high=max, low=min, close=last on each side).
    A derived bar labelled T only contains source bars with timestamp in [T, T+tf), so it never includes the future of
    its own label; it is only *complete* once T+tf has passed. Bins with fewer source bars than expected are flagged
    PARTIAL (kept), and `drop_partial_tail` removes a trailing bin that the data simply ends inside."""
    src = bars["source_timeframe"].iloc[0]
    if TIMEFRAME_MINUTES[target] < TIMEFRAME_MINUTES[src]:
        raise ValueError(f"cannot derive {target} from {src}")
    if target == src:
        return bars.copy()
    expected = TIMEFRAME_MINUTES[target] // TIMEFRAME_MINUTES[src]
    b = bars.sort_values("timestamp_utc")
    key = _bin_keys(b["timestamp_utc"], target, str(b["instrument"].iloc[0]))
    g = b.groupby(key, sort=True)
    agg = {}
    for side in PRICE_SIDES:
        agg[f"{side}_open"] = g[f"{side}_open"].first()
        agg[f"{side}_high"] = g[f"{side}_high"].max()
        agg[f"{side}_low"] = g[f"{side}_low"].min()
        agg[f"{side}_close"] = g[f"{side}_close"].last()
    out = pd.DataFrame(agg)
    out["spread"] = out["ask_close"] - out["bid_close"]
    out["volume"] = g["volume"].sum(min_count=1)
    n = g.size()
    out["quality_flags"] = (n < expected).map({True: "PARTIAL", False: ""})
    out.index.name = "timestamp_utc"
    out = out.reset_index()
    if drop_partial_tail and len(out) and out["quality_flags"].iloc[-1] == "PARTIAL":
        last_src = b["timestamp_utc"].iloc[-1]
        span = pd.Timedelta(minutes=TIMEFRAME_MINUTES[target])
        if target != "D1" and last_src < out["timestamp_utc"].iloc[-1] + span - pd.Timedelta(minutes=TIMEFRAME_MINUTES[src]):
            out = out.iloc[:-1]
    out["instrument"] = b["instrument"].iloc[0]
    out["provider"] = b["provider"].iloc[0]
    out["source_timeframe"] = target
    return out[list(CANONICAL_COLUMNS)].reset_index(drop=True)
