"""Pipeline diagnostic H-demo: proves ResearchSpec -> registry -> data validation -> agent -> costs -> statistics ->
validation separation -> report -> Claude-ready evidence packet on SYNTHETIC fixture data. Claims no edge."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .agent import QuantResearchAgent
from .config import ensure_layout
from .fixtures import synthetic_m1
from .registry import ExperimentRegistry
from .spec import parse_spec
from .storage import MarketStore


def demo_spec_dict(provider: str = "synthetic_fixture") -> dict:
    ts = lambda d: f"2025-{d}T00:00:00+00:00"
    return {
        "title": "Pipeline diagnostic: EMA cross on synthetic random walk",
        "hypothesis": "DIAGNOSTIC ONLY. A fast/slow EMA cross on a synthetic driftless random walk should show no edge after costs; "
                      "this run exists to exercise the research pipeline end to end.",
        "mechanism": {"who_provides_opportunity": "nobody: the data is a synthetic random walk with no counterparty",
                      "why_it_may_persist": "it should not persist; a random walk contains no exploitable structure",
                      "edge_should_exist_when": "never (control case)", "edge_should_disappear_when": "always"},
        "instruments": ["EURUSD"],
        "data": {"provider": provider, "timeframe": "M15", "source_timeframe": "M1", "min_bars_per_period": 100},
        "features": [{"name": "fast", "kind": "ema", "n": 10}, {"name": "slow", "kind": "ema", "n": 40},
                     {"name": "atr14", "kind": "atr", "n": 14}],
        "signal": {"long_when": [{"left": "fast", "op": "crosses_above", "right": "slow"}],
                   "short_when": [{"left": "fast", "op": "crosses_below", "right": "slow"}]},
        "exit": {"atr_feature": "atr14", "stop_atr_mult": 2.0, "target_r": 2.0, "max_hold_bars": 32},
        "costs": {"spread_mult": 1.0, "commission_round_turn": 0.00002, "slippage_per_side": 0.00001, "latency_bars": 0},
        "development": {"start": ts("03-03"), "end": ts("03-10")},
        "validation": {"start": ts("03-10"), "end": ts("03-14")},
        "untouched_test": {"start": ts("03-14"), "end": ts("03-21")},
        "embargo_bars": 40,
        "sensitivity": {"spread_multipliers": [2.0], "slippage_per_side": [0.00005], "parameter_variants": [{"fast": 8}, {"fast": 12}, {"slow": 32}, {"slow": 48}]},
        "rejection": {"min_trades_per_period": 10, "min_net_expectancy_r": 0.0, "max_drawdown_r": 20.0},
        "diagnostic": True,
    }


def run_demo(root: Path | None = None) -> dict:
    root = ensure_layout(root)
    store = MarketStore(root)
    store.write_bars(synthetic_m1(start="2025-02-24", days=26))
    reg = ExperimentRegistry(root / "experiments" / "registry.sqlite")
    agent = QuantResearchAgent(reg, store, root / "reports")
    hid = agent.submit(parse_spec(demo_spec_dict()))
    packet = agent.run(hid)
    return packet


if __name__ == "__main__":
    out = run_demo(Path(sys.argv[1]) if len(sys.argv) > 1 else None)
    print(json.dumps({k: out[k] for k in ("experiment_id", "status", "classification", "claimed_edge", "failure_reason")}, indent=1))
    print(json.dumps({k: out.get(k) for k in ("development", "validation", "untouched_test")}, indent=1, default=str)[:3000])
    print([w["code"] for w in out.get("warnings") or []])
