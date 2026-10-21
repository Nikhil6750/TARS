from __future__ import annotations

import copy

import numpy as np
import pandas as pd
import pytest

import research.features as feat_mod
from research.agent import QuantResearchAgent
from research.antioverfit import multiple_testing, post_run_warnings, pre_run_warnings
from research.backtest import simulate
from research.costs import CostModel
from research.demo import demo_spec_dict, run_demo
from research.features import compute_features, verify_causality
from research.fixtures import synthetic_m1
from research.metrics import compute_metrics
from research.registry import ExperimentRegistry, RegistryError
from research.schema import make_bars
from research.spec import FeatureDef, parse_spec
from research.storage import MarketStore


# ---- cost model ---------------------------------------------------------------------------------------
def test_fills_use_bid_ask_not_mid():
    c = CostModel()
    assert c.entry_fill(1, bid=1.0, ask=1.0002) == 1.0002 and c.entry_fill(-1, 1.0, 1.0002) == 1.0
    assert c.exit_fill(1, 1.0, 1.0002) == 1.0 and c.exit_fill(-1, 1.0, 1.0002) == 1.0002


def test_slippage_commission_stress_and_latency_fields():
    c = CostModel(slippage_per_side=0.0001, commission_round_turn=0.0002, spread_mult=2.0)
    assert c.entry_fill(1, 1.0, 1.0002) == pytest.approx(1.0002 + 0.0001 + 0.0001)  # half of extra spread + slippage
    assert c.adjust_exit(1, 1.0, 0.0002) == pytest.approx(1.0 - 0.0001 - 0.0001)
    assert c.holding_cost(5) == 0.0002 and c.financing_is_placeholder
    assert c.with_stress(spread_mult=3.0).spread_mult == 3.0 and c.spread_mult == 2.0


# ---- backtest on hand-built bars --------------------------------------------------------------------------
def _ramp_bars(n=120, step=0.0005, spread=0.0002):
    ts = pd.date_range("2025-03-03", periods=n, freq="15min", tz="UTC")
    close = 1.0 + step * np.arange(n)
    df = pd.DataFrame({"timestamp_utc": ts, "bid_open": close - step / 2, "bid_high": close + 0.0001, "bid_low": close - step - 0.0001,
                       "bid_close": close})
    for f in ("open", "high", "low", "close"):
        df[f"ask_{f}"] = df[f"bid_{f}"] + spread
    return make_bars(df, instrument="EURUSD", provider="t", source_timeframe="M15")


def _spec_for_ramp(**costs):
    d = copy.deepcopy(demo_spec_dict("t"))
    d["data"].update(timeframe="M15", source_timeframe="M15")
    d["features"] = [{"name": "fast", "kind": "sma", "n": 3}, {"name": "slow", "kind": "sma", "n": 8}, {"name": "atr14", "kind": "atr", "n": 5}]
    d["signal"] = {"long_when": [{"left": "fast", "op": ">", "right": "slow"}], "short_when": []}
    d["exit"] = {"atr_feature": "atr14", "stop_atr_mult": 3.0, "target_r": 1.0, "max_hold_bars": 10}
    d["costs"] = costs
    return parse_spec(d)


def test_long_trades_fill_at_ask_and_report_gross_and_net():
    bars, spec = _ramp_bars(), _spec_for_ramp(commission_round_turn=0.0001)
    t = simulate(bars, spec, spec.costs, start=bars["timestamp_utc"].iloc[0], end=bars["timestamp_utc"].iloc[-1] + pd.Timedelta("15min"))
    assert len(t) > 0 and (t["direction"] == 1).all()
    first = t.iloc[0]
    entry_bar = bars[bars["timestamp_utc"] == first["entry_time"]].iloc[0]
    assert first["entry_price"] == pytest.approx(entry_bar["ask_open"])  # bought at the ask, no mid fantasy
    assert (t["gross_pnl"] > t["net_pnl"]).all()  # costs always reduce net below frictionless gross
    assert (t["cost_r"] > 0).all()
    assert set(t["exit_reason"]) <= {"TARGET", "STOP", "TIME", "PERIOD_END"}


def test_higher_costs_lower_net_but_not_gross():
    bars = _ramp_bars()
    lo, hi = _spec_for_ramp(), _spec_for_ramp(slippage_per_side=0.0003)
    rng = dict(start=bars["timestamp_utc"].iloc[0], end=bars["timestamp_utc"].iloc[-1] + pd.Timedelta("15min"))
    a, b = simulate(bars, lo, lo.costs, **rng), simulate(bars, hi, hi.costs, **rng)
    assert b["net_pnl"].mean() < a["net_pnl"].mean()
    assert b["gross_pnl"].mean() == pytest.approx(a["gross_pnl"].mean())


def test_latency_delays_entry():
    bars = _ramp_bars()
    base, lag = _spec_for_ramp(), _spec_for_ramp(latency_bars=2)
    rng = dict(start=bars["timestamp_utc"].iloc[0], end=bars["timestamp_utc"].iloc[-1] + pd.Timedelta("15min"))
    t0, t2 = simulate(bars, base, base.costs, **rng), simulate(bars, lag, lag.costs, **rng)
    assert t2.iloc[0]["entry_time"] - t2.iloc[0]["signal_time"] == pd.Timedelta("45min")
    assert t0.iloc[0]["entry_time"] - t0.iloc[0]["signal_time"] == pd.Timedelta("15min")


def test_signals_only_inside_period_and_position_closed_at_period_end():
    bars, spec = _ramp_bars(), _spec_for_ramp()
    cut = bars["timestamp_utc"].iloc[60]
    t = simulate(bars, spec, spec.costs, start=bars["timestamp_utc"].iloc[0], end=cut)
    assert (t["signal_time"] < cut).all() and (t["exit_time"] < cut).all()


# ---- look-ahead safeguards -----------------------------------------------------------------------------------
def test_all_builtin_features_are_causal():
    bars = synthetic_m1(days=2)
    defs = [FeatureDef(name=f"f{i}", kind=k, n=n) for i, (k, n) in enumerate(
        [("sma", 10), ("ema", 10), ("atr", 14), ("ret", 5), ("zscore", 20), ("rolling_high", 20), ("rolling_low", 20), ("efficiency_ratio", 10)])]
    defs.append(FeatureDef(name="sp", kind="spread_pips"))
    assert verify_causality(bars, defs) == []


def test_lookahead_detector_catches_a_leaky_feature(monkeypatch):
    bars = synthetic_m1(days=2)
    real = feat_mod.compute_features

    def leaky(b, defs):
        out = real(b, defs)
        out["fast"] = b["bid_close"].shift(-3)  # peeks at the future
        return out
    monkeypatch.setattr(feat_mod, "compute_features", leaky)
    assert "fast" in feat_mod.verify_causality(bars, [FeatureDef(name="fast", kind="sma", n=3)])


# ---- metrics ---------------------------------------------------------------------------------------------------
def _trades(net, gross=None):
    n = len(net)
    return pd.DataFrame({"net_r": net, "gross_r": gross if gross is not None else net,
                         "exit_time": pd.date_range("2025-01-01", periods=n, freq="D", tz="UTC")})


def test_metric_calculations():
    m = compute_metrics(_trades([1.0, -1.0, -1.0, 0.5, 3.0], [1.2, -0.9, -0.9, 0.7, 3.2]))
    assert m["trades"] == 5 and m["net_expectancy_r"] == pytest.approx(0.5) and m["median_r"] == 0.5
    assert m["win_rate"] == pytest.approx(0.6) and m["avg_winner_r"] == pytest.approx(1.5) and m["avg_loser_r"] == -1.0
    assert m["profit_factor"] == pytest.approx(4.5 / 2.0) and m["max_drawdown_r"] == pytest.approx(2.0)
    assert m["max_consecutive_losses"] == 2 and m["worst_trade_r"] == -1.0
    assert m["return_over_drawdown"] == pytest.approx(2.5 / 2.0) and m["max_drawdown_trades"] == 3 and m["max_drawdown_days"] == 4.0
    assert m["gross_expectancy_r"] > m["net_expectancy_r"] and m["cost_drag_r_per_trade"] == pytest.approx(0.16)
    assert compute_metrics(_trades([]))["trades"] == 0


# ---- anti-overfitting ----------------------------------------------------------------------------------------------
def test_multiple_testing_threshold_grows_with_trials():
    assert multiple_testing(1)["critical_abs_t"] < multiple_testing(20)["critical_abs_t"] < multiple_testing(200)["critical_abs_t"]
    assert multiple_testing(1)["critical_abs_t"] == pytest.approx(1.96, abs=0.01)


def test_pre_run_blocks_lookahead_and_flags_search_and_multiple_testing():
    d = copy.deepcopy(demo_spec_dict())
    d["sensitivity"]["parameter_variants"] = [{"fast": 5 + i} for i in range(12)]
    w = {x.code: x for x in pre_run_warnings(parse_spec(d), n_family_trials=7, lookahead_features=["fast"])}
    assert w["LOOKAHEAD"].blocking
    assert {"TOO_MANY_PARAMETER_VARIATIONS", "SINGLE_INSTRUMENT", "MULTIPLE_TESTING_RISK"} <= set(w)


def test_post_run_warnings_cover_degradation_cost_fragility_and_single_year():
    spec = parse_spec(demo_spec_dict())
    dev = {"trades": 5, "net_expectancy_r": 1.0, "gross_expectancy_r": 1.2}
    val = {"trades": 5, "net_expectancy_r": 0.1, "gross_expectancy_r": 0.3, "t_stat": 0.2}
    trades = pd.DataFrame({"net_r": [1.0, 1.0, 1.0], "year": [2024, 2024, 2024]})
    codes = {w.code for w in post_run_warnings(spec, dev=dev, val=val, val_trades=trades, dev_trades=trades.iloc[0:0],
                                               stress={"spread_x2": {"net_expectancy_r": -0.2}},
                                               neighbours=[{"net_expectancy_r": -0.5}, {"net_expectancy_r": -0.2}], n_family_trials=9)}
    assert {"SMALL_SAMPLE", "LARGE_OOS_DEGRADATION", "COST_FRAGILITY", "SINGLE_YEAR_DEPENDENCE",
            "UNSTABLE_NEIGHBOURING_PARAMETERS", "MULTIPLE_TESTING_RISK"} <= codes


# ---- agent end to end -----------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    root = tmp_path_factory.mktemp("data")
    return root, run_demo(root)


def test_demo_runs_whole_pipeline_and_claims_no_edge(demo):
    root, pkt = demo
    assert pkt["experiment_id"] == "H0001" and pkt["claimed_edge"] is False and pkt["diagnostic"] is True
    assert pkt["status"] in {"PRELIMINARY", "REJECTED"} and pkt["untouched_test"] == "NOT_EVALUATED"
    for k in ("development", "validation"):
        assert pkt[k]["trades"] > 0 and "gross_expectancy_r" in pkt[k] and "net_expectancy_r" in pkt[k]
    assert pkt["data_quality"]["EURUSD"]["passed"] and pkt["data_version"].startswith("dv_")
    assert "spread_x2" in pkt["sensitivity"] and pkt["warnings"]
    assert (root / "reports" / "H0001.json").exists() and (root / "reports" / "H0001.md").exists()
    assert len(str(pkt)) < 12000  # compact evidence packet, not raw data


def test_agent_never_loads_untouched_test_period(tmp_path):
    spy_store = MarketStore(tmp_path)
    spy_store.write_bars(synthetic_m1(start="2025-02-24", days=26))
    seen = []
    orig = spy_store.read_bars

    def spy(provider, inst, tf="M1", start=None, end=None):
        seen.append(end)
        return orig(provider, inst, tf, start, end)
    spy_store.read_bars = spy
    reg = ExperimentRegistry(tmp_path / "r.sqlite")
    agent = QuantResearchAgent(reg, spy_store, tmp_path / "rep")
    spec = parse_spec(demo_spec_dict())
    agent.run(agent.submit(spec))
    assert seen and max(seen) <= pd.Timestamp(spec.validation.end)  # test period (starts 03-14) never read


def test_data_quality_failure_blocks_and_is_persisted(tmp_path):
    store = MarketStore(tmp_path)
    bars = synthetic_m1(start="2025-02-24", days=26)
    bars.loc[2000, "bid_high"] = bars.loc[2000, "bid_low"] - 0.01
    store.write_bars(bars)
    reg = ExperimentRegistry(tmp_path / "r.sqlite")
    agent = QuantResearchAgent(reg, store, tmp_path / "rep")
    pkt = agent.run(agent.submit(parse_spec(demo_spec_dict())))
    row = reg.get("H0001")
    assert row["status"] == "FAILED" and "DATA_QUALITY_BLOCK" in row["failure_reason"] and pkt["classification"] == "FAILED_EXPERIMENT"
    assert reg.list("FAILED")  # never deleted


def test_missing_data_fails_cleanly(tmp_path):
    reg = ExperimentRegistry(tmp_path / "r.sqlite")
    agent = QuantResearchAgent(reg, MarketStore(tmp_path), tmp_path / "rep")
    agent.run(agent.submit(parse_spec(demo_spec_dict())))
    assert reg.get("H0001")["status"] == "FAILED"


def test_untouched_test_requires_authorisation_preliminary_status_and_is_one_shot(demo, tmp_path):
    root, _ = demo
    reg = ExperimentRegistry(root / "experiments" / "registry.sqlite")
    agent = QuantResearchAgent(reg, MarketStore(root), root / "reports")
    with pytest.raises(PermissionError):
        agent.run_untouched_test("H0001", authorized=False)
    if reg.get("H0001")["status"] != "PRELIMINARY":
        with pytest.raises(RegistryError):
            agent.run_untouched_test("H0001", authorized=True)
        assert not reg.get("H0001")["test_consumed"]


def test_one_shot_test_flow_on_preliminary_experiment(tmp_path):
    store = MarketStore(tmp_path)
    store.write_bars(synthetic_m1(start="2025-02-24", days=26))
    reg = ExperimentRegistry(tmp_path / "r.sqlite")
    agent = QuantResearchAgent(reg, store, tmp_path / "rep")
    hid = agent.submit(parse_spec(demo_spec_dict()))
    reg.transition(hid, "RUNNING")
    reg.record(hid, results_json={"validation": {"net_expectancy_r": 0.2}})
    reg.transition(hid, "PRELIMINARY")
    pkt = agent.run_untouched_test(hid, authorized=True)
    assert reg.get(hid)["test_consumed"] == 1 and pkt["status"] in {"VALIDATED", "REJECTED"}
    assert isinstance(pkt["untouched_test"], dict)
    with pytest.raises(RegistryError):
        agent.run_untouched_test(hid, authorized=True)
