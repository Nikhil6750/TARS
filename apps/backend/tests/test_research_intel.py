from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from research.agent import QuantResearchAgent
from research.demo import demo_spec_dict
from research.director import (ConclusionClass, DirectorError, ResearchDirector, enforce_epistemics)
from research.fixtures import synthetic_m1
from research.market_state import MarketIntelligenceEngine, compute_market_state
from research.promotion import CandidateStrategy, PromotionDenied, ValidatedStrategy, make_candidate, promote
from research.registry import ExperimentRegistry
from research.spec import parse_spec
from research.storage import MarketStore


def run(coro):
    return asyncio.run(coro)


# ---- market state ----------------------------------------------------------------------------------------------------
def _bars(n=400, shock=0.0, end="2025-03-05 12:00"):
    b = synthetic_m1(start="2025-03-03", days=3)
    b = b[b["timestamp_utc"] <= pd.Timestamp(end, tz="UTC")].iloc[-n:].reset_index(drop=True)
    if shock:
        for c in [c for c in b.columns if c.startswith(("bid_", "ask_")) and c != "bid_none"]:
            b.loc[b.index[-5:], c] = b.loc[b.index[-5:], c] * (1 + shock)
    return b


def _now(b):
    return b["timestamp_utc"].iloc[-1].to_pydatetime() + timedelta(seconds=30)


def test_quiet_market_does_not_escalate():
    b = _bars()
    st = compute_market_state(b, now=_now(b))
    assert st.data_freshness == "FRESH" and not st.requires_reasoning and st.significance < 0.6
    assert st.session in {"ASIA", "LONDON", "LONDON_NY_OVERLAP", "NEWYORK", "OFF"} and st.instrument == "EURUSD"
    assert set(st.returns) == {"1bar", "5bar", "15bar", "60bar"} and st.rolling_volatility and st.volatility_percentile is not None


def test_abnormal_move_with_event_escalates_and_exposes_context():
    b = _bars(shock=0.004)
    now = _now(b)
    ev = [{"title": "US CPI", "at": now + timedelta(minutes=10), "impact": "High"}]
    st = compute_market_state(b, now=now, events=ev)
    assert st.abnormal_move_score >= 3 and st.event_proximity["title"] == "US CPI" and st.requires_reasoning
    assert st.significance >= 0.3 and st.recent_events


def test_unexplained_abnormal_move_alone_escalates_via_watcher_gate():
    b = _bars(shock=0.004)
    st = compute_market_state(b, now=_now(b))
    assert st.abnormal_move_score >= 3 and st.requires_reasoning


def test_stale_data_never_escalates_even_if_move_is_huge():
    b = _bars(shock=0.004)
    st = compute_market_state(b, now=_now(b) + timedelta(hours=3))
    assert st.data_freshness == "STALE" and not st.requires_reasoning and st.confidence < 0.3
    assert "stale" in st.escalation_reason.lower()


def test_empty_bars_are_unavailable():
    st = compute_market_state(_bars().iloc[0:0], now=datetime.now(timezone.utc))
    assert st.data_freshness == "UNAVAILABLE" and not st.requires_reasoning


def test_stale_or_unavailable_chart_is_never_current_evidence():
    b = _bars()
    for ev, ok in (("CURRENT", True), ("STALE", False), ("UNAVAILABLE", False), ("garbage", False)):
        st = compute_market_state(b, now=_now(b), chart_evidence=ev)
        assert (st.chart_evidence == "CURRENT") is ok
        assert bool([n for n in st.notes if "excluded from current evidence" in n]) is (not ok)


def test_engine_collects_only_escalations():
    eng = MarketIntelligenceEngine()
    q, s = _bars(), _bars(shock=0.004)
    eng.update("EURUSD", q, now=_now(q))
    s["instrument"] = "GBPUSD"
    eng.update("GBPUSD", s, now=_now(s))
    assert [x.instrument for x in eng.escalations()] == ["GBPUSD"]


def test_wide_spread_state_and_cross_asset_evidence():
    b = _bars()
    cols = [c for c in b.columns if c.startswith("ask_")]
    b.loc[b.index[-1], cols] += 0.002
    other = _bars(shock=0.004)
    st = compute_market_state(b, now=_now(b), others={"GBPUSD": other})
    assert st.spread_state in {"WIDE", "EXTREME"} and st.cross_asset["GBPUSD"]["abnormal_move_score"] >= 3


# ---- Claude research director ---------------------------------------------------------------------------------------
@pytest.fixture
def lab(tmp_path):
    store = MarketStore(tmp_path)
    store.write_bars(synthetic_m1(start="2025-02-24", days=26))
    reg = ExperimentRegistry(tmp_path / "r.sqlite")
    return reg, QuantResearchAgent(reg, store, tmp_path / "rep")


def _llm(payload):
    calls = []

    async def llm(system, user):
        calls.append((system, user))
        return "Here you go:\n" + json.dumps(payload)
    llm.calls = calls
    return llm


def test_enforce_epistemics_downgrades():
    assert enforce_epistemics(ConclusionClass.VALIDATED_FINDING, ["PRELIMINARY"])[0] is ConclusionClass.UNSUPPORTED_CLAIM
    assert enforce_epistemics(ConclusionClass.VALIDATED_FINDING, ["VALIDATED"])[0] is ConclusionClass.VALIDATED_FINDING
    assert enforce_epistemics(ConclusionClass.PRELIMINARY_EVIDENCE, ["REJECTED"])[0] is ConclusionClass.FAILED_EXPERIMENT


def test_director_review_downgrades_overclaim_and_records(lab):
    reg, agent = lab
    hid = agent.submit(parse_spec(demo_spec_dict()))
    agent.run(hid)
    d = ResearchDirector(_llm({"classification": "VALIDATED_FINDING", "summary": "this works!", "decision": "VALIDATE_NEXT"}), reg, agent)
    res = run(d.review_experiment(hid))
    assert res.output.classification is ConclusionClass.UNSUPPORTED_CLAIM and res.downgraded_from == "VALIDATED_FINDING"
    assert reg.get(hid)["claude_review"][0]["downgraded_from"] == "VALIDATED_FINDING"


def test_director_sends_compact_evidence_not_raw_data(lab):
    reg, agent = lab
    hid = agent.submit(parse_spec(demo_spec_dict()))
    agent.run(hid)
    llm = _llm({"classification": "FAILED_EXPERIMENT", "summary": "rejected", "decision": "REJECT"})
    run(ResearchDirector(llm, reg, agent).diagnose_failure(hid))
    system, user = llm.calls[0]
    assert "lead quantitative researcher" in system and len(user) < 16000 and "bid_open" not in user


def test_director_rejects_oversized_or_malformed(lab):
    reg, agent = lab
    d = ResearchDirector(_llm({"classification": "NOT_A_CLASS", "summary": "x"}), reg, agent)
    with pytest.raises(DirectorError):
        run(d._ask("t", "i", {"ok": 1}))
    with pytest.raises(DirectorError, match="send summaries"):
        run(ResearchDirector(_llm({}), reg, agent)._ask("t", "i", "x" * 20000))


def test_director_propose_goes_through_validation_and_memory(lab):
    reg, agent = lab
    good = {"classification": "HYPOTHESIS", "summary": "an idea", "spec": {**demo_spec_dict(), "diagnostic": False}}
    res = run(ResearchDirector(_llm(good), reg, agent).propose_hypothesis("anything"))
    assert res.experiment_id == "H0001" and res.spec is not None
    # same idea again -> blocked by research memory, nothing new registered
    res2 = run(ResearchDirector(_llm(good), reg, agent).propose_hypothesis("anything"))
    assert res2.experiment_id is None and any("MEMORY_REJECTED" in n for n in res2.notes) and len(reg.list()) == 1
    bad = {"classification": "HYPOTHESIS", "summary": "broken", "spec": {"title": "x"}}
    res3 = run(ResearchDirector(_llm(bad), reg, agent).propose_hypothesis("anything"))
    assert any("SPEC_INVALID" in n for n in res3.notes) and len(reg.list()) == 1


def test_director_exposes_all_six_functions(lab):
    reg, agent = lab
    d = ResearchDirector(_llm({"classification": "HYPOTHESIS", "summary": "ok"}), reg, agent)
    for fn in ("propose_hypothesis", "review_experiment", "diagnose_failure", "design_validation", "compare_experiments", "recommend_next_experiment"):
        assert callable(getattr(d, fn))
    assert {c.value for c in ConclusionClass} == {"HYPOTHESIS", "PRELIMINARY_EVIDENCE", "VALIDATED_FINDING", "UNSUPPORTED_CLAIM", "FAILED_EXPERIMENT"}


# ---- promotion gate ---------------------------------------------------------------------------------------------------
def test_backtest_profit_never_creates_validated_strategy(lab):
    reg, agent = lab
    hid = agent.submit(parse_spec(demo_spec_dict()))
    agent.run(hid)
    with pytest.raises(PromotionDenied):
        promote(reg, hid, approved_by="human")
    with pytest.raises(PromotionDenied, match="only be created by promote"):
        ValidatedStrategy(hid, "h", "d", "me", "now")


def _validated(reg, hid, *, diagnostic=False, trades=40, ok=True, blocking=False):
    reg.transition(hid, "RUNNING")
    res = {"validation": {"net_expectancy_r": 0.2}, "criteria": {"x": {"pass": True}},
           "untouched_test": {"metrics": {"trades": trades}, "criteria": {"t": {"pass": ok}}}}
    reg.record(hid, results_json=res, warnings_json=[{"code": "W", "message": "m", "blocking": blocking}], data_version="dv_x")
    reg.transition(hid, "PRELIMINARY")
    reg.consume_test(hid)
    reg.transition(hid, "VALIDATING")
    reg.transition(hid, "VALIDATED")


def test_promotion_requires_every_condition(lab):
    reg, agent = lab
    d = demo_spec_dict()
    d["diagnostic"] = False
    hid = agent.submit(parse_spec(d))
    _validated(reg, hid)
    assert make_candidate(reg, hid).live_execution_enabled is False
    with pytest.raises(PromotionDenied, match="approver"):
        promote(reg, hid, approved_by=" ")
    strat = promote(reg, hid, approved_by="Arun")
    assert isinstance(strat, ValidatedStrategy) and strat.live_execution_enabled is False and strat.approved_by == "Arun"


@pytest.mark.parametrize("kw,match", [({"trades": 5}, "too small"), ({"ok": False}, "failed criteria"), ({"blocking": True}, "blocking")])
def test_promotion_denied_on_weak_evidence(lab, kw, match):
    reg, agent = lab
    d = demo_spec_dict()
    d["diagnostic"] = False
    hid = agent.submit(parse_spec(d))
    _validated(reg, hid, **kw)
    with pytest.raises(PromotionDenied, match=match):
        promote(reg, hid, approved_by="Arun")


def test_diagnostic_experiments_can_never_be_promoted(lab):
    reg, agent = lab
    hid = agent.submit(parse_spec(demo_spec_dict()))  # diagnostic=True
    _validated(reg, hid)
    with pytest.raises(PromotionDenied, match="diagnostic"):
        promote(reg, hid, approved_by="Arun")


def test_candidate_has_no_authority_and_needs_progress(lab):
    reg, agent = lab
    hid = agent.submit(parse_spec(demo_spec_dict()))
    with pytest.raises(PromotionDenied):
        make_candidate(reg, hid)  # PROPOSED cannot even be a candidate
    assert CandidateStrategy(hid, "h", "PRELIMINARY").live_execution_enabled is False


# ---- live trading stays off ----------------------------------------------------------------------------------------------
def test_live_trading_disabled_and_research_package_never_enables_it(monkeypatch):
    import pathlib

    from trading.execution_guard import live_trading_enabled
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    assert live_trading_enabled() is False
    for p in pathlib.Path(__file__).parents[1].joinpath("research").glob("*.py"):
        code = p.read_text(encoding="utf-8")
        assert "import" not in "".join(l for l in code.splitlines() if "execution_guard" in l), p.name  # never imports the guard
        assert "getenv(" not in code and "LIVE_TRADING" not in code.replace("LIVE_TRADING_ENABLED as permission", "").replace("LIVE_TRADING_ENABLED.", ""), p.name
        assert "order_send(" not in code and "mt5." not in code, p.name
