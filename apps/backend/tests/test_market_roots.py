"""The market data roots are resolved, not hardcoded (task P1-C2).

`market.ingest` used to name `C:/Trade Data` and `D:/TARS_DATA/...`
directly, which made the module specific to the machine it was written on
and restated a convention `research.config` already implements. These tests
cover the two things that matter: that reusing the shared resolver did not
move the store on this machine, and that both roots can actually be
redirected.
"""
from __future__ import annotations

from pathlib import Path

from market import ingest
from research.config import resolve_data_root


def test_the_store_lives_under_the_shared_data_root():
    """Not a path of its own: the market store must follow the same rule as
    every other dataset, including its refusal to fill the system drive."""
    assert ingest.default_store_root() == resolve_data_root() / "market" / "xray_store"


def test_resolving_did_not_move_the_store_on_this_machine():
    """The switch has to be behaviour-preserving where it was verified, or
    the ingested 5.2M bars would silently be orphaned."""
    root = resolve_data_root()
    if root.drive.upper() != "D:":
        import pytest

        pytest.skip(f"data root is {root}; the pre-P1-C2 literal only applies on D:")
    assert ingest.default_store_root() == Path("D:/TARS_DATA/market/xray_store")


def test_the_data_root_override_moves_the_store(monkeypatch, tmp_path):
    monkeypatch.setenv("TARS_DATA_ROOT", str(tmp_path))
    assert ingest.default_store_root() == tmp_path / "market" / "xray_store"


def test_the_csv_root_can_be_redirected(monkeypatch, tmp_path):
    """A machine holding its history elsewhere should not need a code
    change to ingest."""
    monkeypatch.setenv("TARS_MARKET_CSV_ROOT", str(tmp_path))
    assert ingest.csv_root() == tmp_path


def test_the_csv_root_falls_back_to_the_original_location(monkeypatch):
    monkeypatch.delenv("TARS_MARKET_CSV_ROOT", raising=False)
    assert ingest.csv_root() == Path("C:/Trade Data")


def test_blank_overrides_are_ignored_rather_than_producing_an_empty_path(monkeypatch):
    """An env var set to whitespace is a configuration mistake, not a
    request to ingest from the current directory."""
    monkeypatch.setenv("TARS_MARKET_CSV_ROOT", "   ")
    assert ingest.csv_root() == Path("C:/Trade Data")


def test_the_import_time_constants_match_the_resolvers():
    """`CSV_ROOT` and `DEFAULT_STORE_ROOT` are argument defaults, resolved
    once so a root cannot change under a running ingest and write half the
    bars to each place."""
    assert ingest.CSV_ROOT == ingest.csv_root()
    assert ingest.DEFAULT_STORE_ROOT == ingest.default_store_root()


def test_the_store_reads_from_the_resolved_root():
    from market.store import CandleStore

    assert CandleStore().root == ingest.DEFAULT_STORE_ROOT


def test_the_frozen_research_partitions_are_not_the_store():
    """The ingester must never write where quant_brain's frozen experiments
    live, whatever the data root resolves to."""
    store = ingest.default_store_root()
    for frozen in ("local_xauusd", "dukascopy"):
        assert store != resolve_data_root() / "market" / frozen
        assert frozen not in store.parts
