"""Transparent research cost baseline per instrument. Every component is a separate, explicit, reported assumption:
spread (real bid/ask, never modelled), commission, extra slippage, latency, financing. Nothing is hidden in signal logic."""
from __future__ import annotations

import pandas as pd

from .costs import CostModel
from .instruments import get_meta


def baseline_cost_model(instrument: str, *, spread_mult: float = 1.0) -> CostModel:
    d = get_meta(instrument).costs
    return CostModel(spread_mult=spread_mult, commission_round_turn=d.commission_round_turn, slippage_per_side=d.slippage_per_side,
                     latency_bars=d.latency_bars, financing_per_bar=d.financing_per_bar, financing_is_placeholder=not d.financing_enabled)


def cost_baseline_report(instrument: str, bars: pd.DataFrame | None = None) -> dict:
    meta = get_meta(instrument)
    model = baseline_cost_model(instrument)
    rep = {"instrument": meta.instrument, "price_unit": f"{meta.quote_currency} per {meta.contract_unit}",
           "tick_size": meta.tick_size, "pip_size": meta.pip_size,
           "spread": "REAL observed bid/ask on every fill (no modelled mid fills)",
           "commission_round_turn": model.commission_round_turn, "slippage_per_side": model.slippage_per_side,
           "latency_bars": model.latency_bars,
           "financing": ("DISABLED placeholder - intraday research only; swap/rollover is NOT modelled"
                         if model.financing_is_placeholder else model.financing_per_bar),
           "assumption_notes": meta.costs.notes}
    if bars is not None and len(bars):
        sp = (bars["ask_close"] - bars["bid_close"]).astype(float)
        rep["observed_spread"] = {"median": float(sp.median()), "mean": float(sp.mean()), "p95": float(sp.quantile(0.95)),
                                  "p99": float(sp.quantile(0.99)), "max": float(sp.max()),
                                  "median_in_pips": float(sp.median() / meta.pip_size) if meta.pip_size else None}
    return rep
