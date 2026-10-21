"""Causal feature construction. Every feature at bar t depends only on bars <= t (rolling windows never use
negative shifts; rolling_high/low are shifted by one so they exclude the current bar). `verify_causality`
proves this empirically by recomputing on truncated history."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .schema import pip_size
from .spec import Condition, FeatureDef


def mid_ohlc(bars: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({f: (bars[f"bid_{f}"] + bars[f"ask_{f}"]) / 2 for f in ("open", "high", "low", "close")})


def _atr(m: pd.DataFrame, n: int) -> pd.Series:
    pc = m["close"].shift(1)
    tr = pd.concat([m["high"] - m["low"], (m["high"] - pc).abs(), (m["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def compute_features(bars: pd.DataFrame, defs: list[FeatureDef]) -> pd.DataFrame:
    m = mid_ohlc(bars)
    c = m["close"]
    out = pd.DataFrame(index=bars.index)
    out["close"] = c
    inst = str(bars["instrument"].iloc[0]) if len(bars) else "EURUSD"
    for d in defs:
        n = d.n
        if d.kind == "close":
            s = c
        elif d.kind == "sma":
            s = c.rolling(n, min_periods=n).mean()
        elif d.kind == "ema":
            s = c.ewm(span=n, adjust=False, min_periods=n).mean()
        elif d.kind == "atr":
            s = _atr(m, n)
        elif d.kind == "ret":
            s = np.log(c / c.shift(n))
        elif d.kind == "zscore":
            s = (c - c.rolling(n, min_periods=n).mean()) / c.rolling(n, min_periods=n).std()
        elif d.kind == "rolling_high":
            s = m["high"].rolling(n, min_periods=n).max().shift(1)
        elif d.kind == "rolling_low":
            s = m["low"].rolling(n, min_periods=n).min().shift(1)
        elif d.kind == "efficiency_ratio":
            s = (c - c.shift(n)).abs() / c.diff().abs().rolling(n, min_periods=n).sum()
        elif d.kind == "spread_pips":
            s = (bars["ask_close"] - bars["bid_close"]) / pip_size(inst)
        else:  # pragma: no cover - spec validation prevents this
            raise ValueError(d.kind)
        out[d.name] = s
    return out


def evaluate_conditions(feats: pd.DataFrame, conds: list[Condition]) -> pd.Series:
    """AND of all conditions; NaN anywhere -> False."""
    ok = pd.Series(True, index=feats.index)
    if not conds:
        return pd.Series(False, index=feats.index)
    for cd in conds:
        left = feats[cd.left]
        right = feats[cd.right] if isinstance(cd.right, str) else pd.Series(float(cd.right), index=feats.index)
        if cd.op == ">":
            r = left > right
        elif cd.op == ">=":
            r = left >= right
        elif cd.op == "<":
            r = left < right
        elif cd.op == "<=":
            r = left <= right
        elif cd.op == "crosses_above":
            r = (left > right) & (left.shift(1) <= right.shift(1))
        else:
            r = (left < right) & (left.shift(1) >= right.shift(1))
        ok &= r.fillna(False) & left.notna() & right.notna()
    return ok


def verify_causality(bars: pd.DataFrame, defs: list[FeatureDef], samples: int = 6) -> list[str]:
    """Return the names of features whose value at bar t changes when future bars are removed (look-ahead)."""
    full = compute_features(bars, defs)
    n = len(bars)
    if n < 20:
        return []
    bad: set[str] = set()
    for i in np.linspace(n // 3, n - 1, samples).astype(int):
        trunc = compute_features(bars.iloc[: i + 1].reset_index(drop=True), defs)
        for col in full.columns:
            a, b = full[col].iloc[i], trunc[col].iloc[-1]
            if not (pd.isna(a) and pd.isna(b)) and not np.isclose(a, b, rtol=1e-9, atol=1e-12, equal_nan=True):
                bad.add(col)
    return sorted(bad)
