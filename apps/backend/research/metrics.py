"""Standard research metrics. Win rate is reported but is never an objective."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _max_consecutive_losses(r: np.ndarray) -> int:
    best = cur = 0
    for x in r:
        cur = cur + 1 if x < 0 else 0
        best = max(best, cur)
    return best


def _drawdown(r: np.ndarray, times: pd.Series) -> tuple[float, int, float]:
    """(max drawdown in R, longest underwater stretch in trades, longest underwater stretch in days)."""
    if len(r) == 0:
        return 0.0, 0, 0.0
    t = list(times)
    eq = np.cumsum(r)
    peak, peak_i, peak_t = 0.0, -1, t[0]
    max_dd, longest, longest_days = 0.0, 0, 0.0
    for k, v in enumerate(eq):
        if v >= peak:
            if k - peak_i > 1 or peak_i < k - 1:
                pass
            longest = max(longest, k - peak_i - 1)
            longest_days = max(longest_days, (t[k] - peak_t).total_seconds() / 86400 if peak_i >= 0 or k else 0.0)
            peak, peak_i, peak_t = v, k, t[k]
        max_dd = max(max_dd, peak - v)
    longest = max(longest, len(eq) - 1 - peak_i)
    longest_days = max(longest_days, (t[-1] - peak_t).total_seconds() / 86400)
    return float(max_dd), int(longest), float(longest_days)


def compute_metrics(trades: pd.DataFrame) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    net = trades["net_r"].to_numpy(dtype=float)
    gross = trades["gross_r"].to_numpy(dtype=float)
    wins, losses = net[net > 0], net[net <= 0]
    gp, gl = wins.sum(), -losses.sum()
    max_dd, dd_trades, dd_days = _drawdown(net, trades["exit_time"])
    sd = float(net.std(ddof=1)) if n > 1 else float("nan")
    tstat = float(net.mean() / (sd / math.sqrt(n))) if n > 1 and sd > 0 else float("nan")
    k = max(int(math.ceil(0.05 * n)), 1)
    total = float(net.sum())
    return {
        "trades": n,
        "net_expectancy_r": float(net.mean()), "gross_expectancy_r": float(gross.mean()),
        "avg_r": float(net.mean()), "median_r": float(np.median(net)),
        "win_rate": float((net > 0).mean()),
        "avg_winner_r": float(wins.mean()) if len(wins) else 0.0,
        "avg_loser_r": float(losses.mean()) if len(losses) else 0.0,
        "profit_factor": float(gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0.0),
        "net_total_r": total, "gross_total_r": float(gross.sum()),
        "cost_drag_r_per_trade": float((gross - net).mean()),
        "max_drawdown_r": max_dd, "max_drawdown_trades": dd_trades, "max_drawdown_days": dd_days,
        "return_over_drawdown": float(total / max_dd) if max_dd > 0 else (float("inf") if total > 0 else 0.0),
        "tail_loss_cvar5_r": float(np.sort(net)[:k].mean()), "worst_trade_r": float(net.min()),
        "max_consecutive_losses": _max_consecutive_losses(net),
        "t_stat": tstat, "p_value_two_sided": _p_from_t(tstat),
    }


def _p_from_t(t: float) -> float:
    if t is None or (isinstance(t, float) and math.isnan(t)):
        return float("nan")
    return float(math.erfc(abs(t) / math.sqrt(2)))  # normal approximation


def breakdown(trades: pd.DataFrame, column: str) -> dict:
    out = {}
    if len(trades) == 0:
        return out
    for key, g in trades.groupby(column):
        out[str(key)] = {"trades": int(len(g)), "net_expectancy_r": float(g["net_r"].mean()),
                         "gross_expectancy_r": float(g["gross_r"].mean()), "net_total_r": float(g["net_r"].sum()),
                         "win_rate": float((g["net_r"] > 0).mean())}
    return out


def regime_breakdowns(trades: pd.DataFrame, dimensions: list[str]) -> dict:
    t = trades.copy()
    if len(t):
        t["direction"] = t["direction"].map({1: "LONG", -1: "SHORT"})
    return {d: breakdown(t, d) for d in dimensions}


def clean(obj):
    """JSON-safe: inf/nan -> None-ish strings, numpy -> python, Timestamps -> str."""
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        v = float(obj)
        return "inf" if math.isinf(v) and v > 0 else ("-inf" if math.isinf(v) else (None if math.isnan(v) else v))
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (pd.Timestamp,)):
        return str(obj)
    return obj
