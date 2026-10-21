from __future__ import annotations

import copy
import sqlite3

import pytest

from research.agent import QuantResearchAgent, ResearchRejected
from research.demo import demo_spec_dict
from research.memory import ResearchMemory
from research.registry import ExperimentRegistry, RegistryError
from research.spec import ResearchSpecError, parse_spec
from research.storage import MarketStore


def _spec(**over):
    d = copy.deepcopy(demo_spec_dict())
    d.update(over)
    return parse_spec(d)


@pytest.fixture
def reg(tmp_path):
    return ExperimentRegistry(tmp_path / "reg.sqlite")


# ---- ResearchSpec validation ------------------------------------------------------------------------
def test_valid_spec_parses_and_hash_is_stable():
    a, b = _spec(), _spec()
    assert a.spec_hash() == b.spec_hash() and a.instruments == ["EURUSD"]


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d["features"].append({"name": "fut", "kind": "lead", "n": 3}), "not allowed"),
    (lambda d: d["signal"]["long_when"].append({"left": "ghost", "op": ">", "right": 1}), "undefined feature"),
    (lambda d: d["validation"].update(start="2025-03-05T00:00:00+00:00"), "ordered"),
    (lambda d: d["development"].update(start="2025-03-03T00:00:00"), "timezone"),
    (lambda d: d["exit"].update(atr_feature="fast"), "atr"),
    (lambda d: d.update(unexpected_field=1), "Extra"),
    (lambda d: d["sensitivity"].update(parameter_variants=[{"fast": i + 2} for i in range(31)]), "unrestricted search"),
    (lambda d: d["features"][0].update(n=None), "requires n"),
    (lambda d: d.update(differs_from=["H0001"]), "differs_because"),
])
def test_invalid_specs_rejected(mutate, needle):
    d = copy.deepcopy(demo_spec_dict())
    mutate(d)
    with pytest.raises(ResearchSpecError) as e:
        parse_spec(d)
    assert needle.lower() in str(e.value).lower()


# ---- registry ----------------------------------------------------------------------------------------
def test_ids_are_sequential_and_persistent(reg, tmp_path):
    a = reg.register(_spec())
    b = reg.register(_spec(title="Another diagnostic title"))
    assert (a, b) == ("H0001", "H0002")
    again = ExperimentRegistry(tmp_path / "reg.sqlite")
    assert [r["id"] for r in again.list()] == ["H0001", "H0002"]
    got = again.get("H0001")
    assert got["status"] == "PROPOSED" and got["mechanism"]["who_provides_opportunity"] and got["git_version"]
    assert again.spec("H0001").spec_hash() == got["spec_hash"]


def test_definition_is_immutable_and_rows_undeletable(reg, tmp_path):
    hid = reg.register(_spec())
    con = sqlite3.connect(tmp_path / "reg.sqlite")
    with pytest.raises(sqlite3.DatabaseError, match="frozen"):
        con.execute("UPDATE experiments SET spec_json='{}' WHERE id=?", (hid,))
    with pytest.raises(sqlite3.DatabaseError, match="frozen"):
        con.execute("UPDATE experiments SET id='H9999' WHERE id=?", (hid,))
    with pytest.raises(sqlite3.DatabaseError, match="permanent"):
        con.execute("DELETE FROM experiments WHERE id=?", (hid,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("DELETE FROM experiment_events")


def test_status_machine_and_failed_experiments_persist(reg):
    hid = reg.register(_spec())
    with pytest.raises(RegistryError):
        reg.transition(hid, "VALIDATED")
    reg.transition(hid, "RUNNING")
    reg.record(hid, failure_reason="DATA_QUALITY_BLOCK: x")
    reg.transition(hid, "FAILED", "x")
    with pytest.raises(RegistryError):
        reg.transition(hid, "RUNNING")  # terminal
    row = reg.get(hid)
    assert row["status"] == "FAILED" and row["failure_reason"].startswith("DATA_QUALITY")
    assert [h["event"] for h in reg.history(hid)] == ["REGISTERED", "STATUS:PROPOSED->RUNNING", "STATUS:RUNNING->FAILED"]


def test_untouched_test_can_be_consumed_only_once(reg):
    hid = reg.register(_spec())
    reg.consume_test(hid)
    with pytest.raises(RegistryError, match="already consumed"):
        reg.consume_test(hid)


# ---- research memory ---------------------------------------------------------------------------------
def _real(**over):
    return _spec(diagnostic=False, **over)


def _dead(reg, spec=None):
    hid = reg.register(spec or _real())
    reg.transition(hid, "REJECTED", "test")
    return hid


def test_memory_blocks_repeating_rejected_research(reg):
    old = _dead(reg)
    verdict = ResearchMemory(reg).check(_real())
    assert not verdict.allowed and old in verdict.reason


def test_memory_allows_materially_different_rejected_follow_up(reg):
    old = _dead(reg)
    d = copy.deepcopy(demo_spec_dict())
    d["diagnostic"] = False
    d["mechanism"] = {"who_provides_opportunity": "liquidity providers widening quotes around scheduled central bank announcements",
                      "why_it_may_persist": "inventory risk transfer is compensated and recurring each policy cycle",
                      "edge_should_exist_when": "policy surprise creates one-sided flow", "edge_should_disappear_when": "event risk is fully priced"}
    d["differs_from"] = [old]
    d["differs_because"] = "uses an event-flow mechanism instead of pure trend-following on noise"
    assert ResearchMemory(reg).check(parse_spec(d)).allowed


def test_memory_blocks_live_duplicate_and_ignores_unrelated(reg):
    reg.register(_real())
    assert not ResearchMemory(reg).check(_real()).allowed
    other = copy.deepcopy(demo_spec_dict())
    other["diagnostic"] = False
    other["instruments"] = ["USDJPY"]
    assert ResearchMemory(reg).check(parse_spec(other)).allowed


def test_agent_submit_refuses_rejected_repeat(reg, tmp_path):
    _dead(reg)
    agent = QuantResearchAgent(reg, MarketStore(tmp_path), tmp_path / "reports")
    with pytest.raises(ResearchRejected):
        agent.submit(_real())
    assert len(reg.list()) == 1  # nothing new registered


def test_diagnostics_never_block_real_hypotheses(reg):
    _dead(reg, _spec())  # a rejected DIAGNOSTIC carries no research claim
    assert ResearchMemory(reg).check(_real()).allowed
