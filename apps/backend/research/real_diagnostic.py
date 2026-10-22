"""REAL_DATA_DIAGNOSTIC: a deliberately neutral pipeline check on real provider data. It claims no edge and can never be
promoted. Its only job is to prove provider -> canonical -> quality -> DuckDB -> Quant Agent -> costs -> report."""
from __future__ import annotations

import json
import sys
import time
from datetime import timedelta
from pathlib import Path

import pandas as pd

from .agent import QuantResearchAgent, render_markdown
from .config import resolve_data_root
from .cost_baseline import baseline_cost_model, cost_baseline_report
from .features import compute_features, evaluate_conditions
from .backtest import simulate
from .metrics import compute_metrics, regime_breakdowns
from .quality import gap_summary, validate_bars
from .registry import ExperimentRegistry
from .schema import derive_timeframe
from .spec import ResearchSpec, parse_spec
from .storage import MarketStore


def real_diagnostic_spec(instrument: str, data_start: pd.Timestamp, data_end: pd.Timestamp, provider: str = "dukascopy") -> ResearchSpec:
    """Chronological 60/25/15 split of the available range (warm-up handled by the agent from earlier data)."""
    span = data_end - data_start
    d0 = (data_start + timedelta(days=1)).normalize()
    d1 = (data_start + span * 0.60).normalize()
    v1 = (data_start + span * 0.85).normalize()
    t1 = data_end.normalize() + timedelta(days=1)
    iso = lambda t: t.isoformat()
    return parse_spec({
        "title": f"REAL_DATA_DIAGNOSTIC: pipeline check on {instrument} M1 bid/ask ({provider})",
        "hypothesis": "DIAGNOSTIC ONLY - NO HYPOTHESIS ABOUT MARKET BEHAVIOUR IS BEING TESTED. A fast/slow EMA cross is run on real "
                      "provider data solely to prove the research pipeline end to end; any result is uninterpretable as an edge.",
        "mechanism": {"who_provides_opportunity": "none claimed: this is a pipeline check, not a market hypothesis",
                      "why_it_may_persist": "no persistence is claimed or tested", "edge_should_exist_when": "never claimed",
                      "edge_should_disappear_when": "not applicable"},
        "instruments": [instrument],
        "data": {"provider": provider, "timeframe": "M15", "source_timeframe": "M1", "min_bars_per_period": 200},
        "features": [{"name": "fast", "kind": "ema", "n": 10}, {"name": "slow", "kind": "ema", "n": 40}, {"name": "atr14", "kind": "atr", "n": 14}],
        "signal": {"long_when": [{"left": "fast", "op": "crosses_above", "right": "slow"}],
                   "short_when": [{"left": "fast", "op": "crosses_below", "right": "slow"}]},
        "exit": {"atr_feature": "atr14", "stop_atr_mult": 2.0, "target_r": 2.0, "max_hold_bars": 32},
        "costs": baseline_cost_model(instrument).model_dump(),
        "development": {"start": iso(d0), "end": iso(d1)}, "validation": {"start": iso(d1), "end": iso(v1)},
        "final_untouched": {"start": iso(v1), "end": iso(t1)},
        "embargo_bars": 40,
        "sensitivity": {"spread_multipliers": [2.0], "slippage_per_side": [0.10], "parameter_variants": [{"fast": 8}, {"fast": 12}, {"slow": 32}, {"slow": 48}]},
        "rejection": {"min_trades_per_period": 20, "min_net_expectancy_r": 0.0, "max_drawdown_r": 25.0},
        "diagnostic": True, "diagnostic_kind": "REAL_DATA_DIAGNOSTIC",
    })


def benchmark(spec: ResearchSpec, root: Path) -> dict:
    """Stage timings of the CURRENT plain-python path on real data (no optimisation applied)."""
    t = {}
    store = MarketStore(root)
    tic = time.perf_counter()
    inst = spec.instruments[0]
    bars = store.read_bars(spec.data.provider, inst, "M1", end=pd.Timestamp(spec.validation.end))
    t["data_load_s"] = time.perf_counter() - tic
    tic = time.perf_counter(); rep = validate_bars(bars); t["quality_s"] = time.perf_counter() - tic
    tic = time.perf_counter(); m15 = derive_timeframe(bars, "M15", drop_partial_tail=True); t["derive_m15_s"] = time.perf_counter() - tic
    tic = time.perf_counter(); feats = compute_features(m15, spec.features); t["features_s"] = time.perf_counter() - tic
    tic = time.perf_counter()
    sig = evaluate_conditions(feats, spec.signal.long_when) | evaluate_conditions(feats, spec.signal.short_when)
    t["signals_s"] = time.perf_counter() - tic
    d0, v1 = pd.Timestamp(spec.development.start), pd.Timestamp(spec.validation.end)
    tic = time.perf_counter(); trades = simulate(m15, spec, spec.costs, start=d0, end=v1, feats=feats); t["backtest_s"] = time.perf_counter() - tic
    tic = time.perf_counter(); m = compute_metrics(trades); t["metrics_s"] = time.perf_counter() - tic
    tic = time.perf_counter(); regime_breakdowns(trades, spec.regime_tests); t["regimes_s"] = time.perf_counter() - tic
    t["m1_rows"], t["m15_bars"], t["signals"], t["trades"] = len(bars), len(m15), int(sig.sum()), int(m.get("trades", 0))
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in t.items()}


def run(root: Path | None = None) -> dict:
    root = Path(root) if root else resolve_data_root()
    store = MarketStore(root)
    bars = store.read_bars("dukascopy", "XAUUSD", "M1", end=store.final_start("XAUUSD"))
    if bars.empty:
        raise SystemExit("no XAUUSD data ingested")
    spec = real_diagnostic_spec("XAUUSD", bars["timestamp_utc"].iloc[0], bars["timestamp_utc"].iloc[-1])
    out = {"data_rows": len(bars), "cost_baseline": cost_baseline_report("XAUUSD", bars)}
    out["benchmark"] = benchmark(spec, root)
    reg = ExperimentRegistry(root / "experiments" / "registry.sqlite")
    agent = QuantResearchAgent(reg, store, root / "reports")
    tic = time.perf_counter()
    hid = agent.submit(spec)
    packet = agent.run(hid)
    out["full_experiment_s"] = round(time.perf_counter() - tic, 3)
    tic = time.perf_counter()
    agent.evidence_packet(hid)
    render_markdown(packet)
    out["benchmark"]["report_generation_s"] = round(time.perf_counter() - tic, 4)
    out["experiment_id"], out["packet"] = hid, packet
    (root / "reports" / f"{hid}_cost_baseline.json").write_text(json.dumps(out["cost_baseline"], indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    res = run()
    p = res["packet"]
    print(json.dumps({k: res[k] for k in ("experiment_id", "full_experiment_s", "benchmark")}, indent=1))
    print(json.dumps({k: p.get(k) for k in ("status", "classification", "claimed_edge", "diagnostic_kind", "failure_reason", "final_data")}, indent=1, default=str))
    print(json.dumps({k: p.get(k) for k in ("development", "validation", "data_quality")}, indent=1, default=str)[:2500])
    print([w["code"] for w in p.get("warnings") or []])
