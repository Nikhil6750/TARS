"""Tiny deterministic SYNTHETIC bar fixtures for tests and the pipeline diagnostic. Never evidence about markets."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .schema import make_bars


def synthetic_m1(instrument: str = "EURUSD", start: str = "2025-03-03", days: int = 15, seed: int = 7,
                 base: float = 1.08, vol: float = 0.00004, spread: float = 0.0001, provider: str = "synthetic_fixture") -> pd.DataFrame:
    """Weekday-only M1 bid/ask random walk (Mon 00:00 - Fri 21:59 UTC)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=days * 24 * 60 * 2, freq="1min", tz="UTC")
    idx = idx[(idx.dayofweek < 4) | ((idx.dayofweek == 4) & (idx.hour < 22))]
    first = idx[0].normalize()
    last = first + pd.Timedelta(days=days)
    idx = idx[idx < last]
    n = len(idx)
    ret = rng.normal(0, vol, n)
    close = base * np.exp(np.cumsum(ret))
    open_ = np.concatenate([[base], close[:-1]])
    hi = np.maximum(open_, close) + np.abs(rng.normal(0, vol / 2, n)) * base
    lo = np.minimum(open_, close) - np.abs(rng.normal(0, vol / 2, n)) * base
    sp = spread * (1 + 0.2 * rng.random(n))
    df = pd.DataFrame({"timestamp_utc": idx,
                       "bid_open": open_, "bid_high": hi, "bid_low": lo, "bid_close": close,
                       "ask_open": open_ + sp, "ask_high": hi + sp, "ask_low": lo + sp, "ask_close": close + sp})
    df["spread"] = df["ask_close"] - df["bid_close"]
    df["volume"] = rng.integers(10, 200, n).astype(float)
    return make_bars(df, instrument=instrument, provider=provider)
