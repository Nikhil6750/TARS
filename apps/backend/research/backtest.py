"""Deterministic bar-based backtest on real bid/ask. One position per instrument at a time.

Timing: signal evaluated at bar i close -> fill at open of bar i+1+latency (long buys ask, short sells bid).
Stops/targets trigger on the side that would actually transact (long exits on bid, short on ask); if stop and
target are both touched in one bar the STOP is assumed first. Gaps through a level fill at the open.
Gross = frictionless mid-to-mid result of the same trade; net = real fills + slippage + commission + financing."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .costs import CostModel
from .features import compute_features, evaluate_conditions, mid_ohlc
from .sessions import classify_sessions
from .spec import ResearchSpec

def regime_context(bars: pd.DataFrame, atr: pd.Series) -> pd.DataFrame:
    """Causal regime tags per bar (no future data): ATR percentile vs trailing window, efficiency ratio."""
    m = mid_ohlc(bars)
    vol_pct = atr.rolling(500, min_periods=50).rank(pct=True)
    er = (m["close"] - m["close"].shift(20)).abs() / m["close"].diff().abs().rolling(20, min_periods=20).sum()
    vol = pd.Series(np.where(vol_pct.isna(), "UNKNOWN", np.where(vol_pct < 1 / 3, "LOW", np.where(vol_pct > 2 / 3, "HIGH", "MID"))),
                    index=bars.index)
    tr = pd.Series(np.where(er.isna(), "UNKNOWN", np.where(er > 0.3, "TREND", "RANGE")), index=bars.index)
    return pd.DataFrame({"volatility": vol, "trend_range": tr})


def simulate(bars: pd.DataFrame, spec: ResearchSpec, costs: CostModel, *, start: pd.Timestamp, end: pd.Timestamp,
             feats: pd.DataFrame | None = None, embargo_bars: int = 0) -> pd.DataFrame:
    """Trades whose signal falls in [start, end). Open positions are force-closed at the last bar before `end`."""
    bars = bars.reset_index(drop=True)
    feats = compute_features(bars, spec.features) if feats is None else feats
    ts = bars["timestamp_utc"]
    in_period = ((ts >= start) & (ts < end)).to_numpy()
    idx_in = np.flatnonzero(in_period)
    if len(idx_in) == 0:
        return _empty()
    first_ok = idx_in[0] + embargo_bars
    last_bar = idx_in[-1]
    long_sig = evaluate_conditions(feats, spec.signal.long_when).to_numpy()
    short_sig = evaluate_conditions(feats, spec.signal.short_when).to_numpy()
    atr = feats[spec.exit.atr_feature].to_numpy()
    reg = regime_context(bars, feats[spec.exit.atr_feature])
    sessions = classify_sessions(ts).to_numpy()
    a = {k: bars[k].to_numpy(dtype=float) for k in ("bid_open", "bid_high", "bid_low", "bid_close",
                                                     "ask_open", "ask_high", "ask_low", "ask_close")}
    ex = spec.exit
    trades: list[dict] = []
    i = first_ok
    while i < last_bar:
        direction = 1 if long_sig[i] else (-1 if short_sig[i] else 0)
        j = i + 1 + costs.latency_bars
        if direction == 0 or j > last_bar or not np.isfinite(atr[i]) or atr[i] <= 0:
            i += 1
            continue
        risk = ex.stop_atr_mult * atr[i]
        raw_entry = a["ask_open"][j] if direction > 0 else a["bid_open"][j]
        entry_fill = costs.entry_fill(direction, a["bid_open"][j], a["ask_open"][j])
        entry_mid = (a["bid_open"][j] + a["ask_open"][j]) / 2
        stop = raw_entry - direction * risk
        target = raw_entry + direction * ex.target_r * risk if ex.target_r else None
        exit_level, exit_bar, reason = None, None, "TIME"
        k_end = min(j + ex.max_hold_bars - 1, last_bar)
        for k in range(j, k_end + 1):
            if direction > 0:
                if a["bid_low"][k] <= stop:
                    exit_level, reason = min(stop, a["bid_open"][k]), "STOP"
                elif target is not None and a["bid_high"][k] >= target:
                    exit_level, reason = max(target, a["bid_open"][k]), "TARGET"
            else:
                if a["ask_high"][k] >= stop:
                    exit_level, reason = max(stop, a["ask_open"][k]), "STOP"
                elif target is not None and a["ask_low"][k] <= target:
                    exit_level, reason = min(target, a["ask_open"][k]), "TARGET"
            if exit_level is not None:
                exit_bar = k
                break
        if exit_level is None:
            exit_bar = k_end
            exit_level = a["bid_close"][k_end] if direction > 0 else a["ask_close"][k_end]
            reason = "PERIOD_END" if j + ex.max_hold_bars - 1 > last_bar else "TIME"
        spread_x = a["ask_close"][exit_bar] - a["bid_close"][exit_bar]
        exit_fill = costs.adjust_exit(direction, exit_level, spread_x)
        exit_mid = exit_level + direction * spread_x / 2  # long exits on bid (mid = bid + s/2); short on ask (mid = ask - s/2)
        held = exit_bar - j + 1
        gross = direction * (exit_mid - entry_mid)
        net = direction * (exit_fill - entry_fill) - costs.holding_cost(held)
        t_sig = ts.iloc[i]
        trades.append({
            "signal_time": t_sig, "entry_time": ts.iloc[j], "exit_time": ts.iloc[exit_bar], "direction": direction,
            "entry_price": entry_fill, "exit_price": exit_fill, "risk": risk, "bars_held": held, "exit_reason": reason,
            "gross_pnl": gross, "net_pnl": net, "gross_r": gross / risk, "net_r": net / risk,
            "cost_r": (gross - net) / risk, "spread_at_entry": a["ask_open"][j] - a["bid_open"][j],
            "session": sessions[i], "volatility": reg["volatility"].iloc[i],
            "trend_range": reg["trend_range"].iloc[i], "year": int(ts.iloc[exit_bar].year),
        })
        i = exit_bar + 1
    return pd.DataFrame(trades) if trades else _empty()


TRADE_COLUMNS = ["signal_time", "entry_time", "exit_time", "direction", "entry_price", "exit_price", "risk", "bars_held",
                 "exit_reason", "gross_pnl", "net_pnl", "gross_r", "net_r", "cost_r", "spread_at_entry", "session",
                 "volatility", "trend_range", "year"]


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=TRADE_COLUMNS)
