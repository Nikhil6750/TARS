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
PIP_SIZE = {"XAUUSD": 0.1, "XAGUSD": 0.01}  # JPY pairs 0.01; other FX 0.0001 (see pip_size)

ARROW_SCHEMA = pa.schema([
    ("timestamp_utc", pa.timestamp("ns", tz="UTC")), ("instrument", pa.string()), ("provider", pa.string()),
    *[(c, pa.float64()) for c in PRICE_COLUMNS], ("spread", pa.float64()), ("volume", pa.float64()),
    ("source_timeframe", pa.string()), ("quality_flags", pa.string()),
])


def pip_size(instrument: str) -> float:
    instrument = instrument.upper()
    if instrument in PIP_SIZE:
        return PIP_SIZE[instrument]
    return 0.01 if instrument.endswith("JPY") else 0.0001


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


def derive_timeframe(bars: pd.DataFrame, target: str) -> pd.DataFrame:
    """Deterministic resample (left-closed, left-labelled; open=first, high=max, low=min, close=last on
    each side). Bins with fewer source bars than expected get a PARTIAL flag but are kept."""
    src = bars["source_timeframe"].iloc[0]
    if TIMEFRAME_MINUTES[target] < TIMEFRAME_MINUTES[src]:
        raise ValueError(f"cannot derive {target} from {src}")
    if target == src:
        return bars.copy()
    expected = TIMEFRAME_MINUTES[target] // TIMEFRAME_MINUTES[src]
    b = bars.sort_values("timestamp_utc")
    key = b["timestamp_utc"].dt.floor(f"{TIMEFRAME_MINUTES[target]}min")
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
    out["quality_flags"] = (g.size() < expected).map({True: "PARTIAL", False: ""})
    out.index.name = "timestamp_utc"
    out = out.reset_index()
    out["instrument"] = b["instrument"].iloc[0]
    out["provider"] = b["provider"].iloc[0]
    out["source_timeframe"] = target
    return out[list(CANONICAL_COLUMNS)]
