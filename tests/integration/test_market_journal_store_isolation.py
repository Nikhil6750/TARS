"""Confirmed release gate: a new store must not inherit another store's journal.

These are synthetic unit fixtures, never product/demo market data. Real-package
reproduction returned 39 old AUDCHF rows under a gold-only Dukascopy provenance.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from market.orderblocks import ZoneEvent
from market.service import MarketXRayService
from market.state import MarketState
from market.store import CandleStore
from market.universe import TIMEFRAMES


@pytest.mark.parametrize("old_symbol", ["AUDCHF", "XAUUSD"])
def test_named_replay_store_does_not_claim_legacy_events_from_another_dataset(tmp_path, old_symbol):
    # A persisted row from the previously active store, as in an upgraded user's
    # shared Market database. The old database has no dataset/source columns.
    state = MarketState(tmp_path / "shared.db")
    state.record_events([ZoneEvent(zone_id="previous-store-zone", symbol=old_symbol,
                                  timeframe="H1", kind="TAPPED",
                                  ts="2026-01-01T08:00:00+00:00", price=1.0)])
    root = tmp_path / "gold-store"
    generation = root / "generations" / "gold-replay" / "XAUUSD"
    generation.mkdir(parents=True)
    for timeframe, minutes in TIMEFRAMES.items():
        pd.DataFrame({"ts": pd.date_range("2026-01-01", periods=576, freq=f"{minutes}min"),
                      "open": 4000., "high": 4001., "low": 3999., "close": 4000.,
                      "spread": .2, "volume": 1.}).to_parquet(generation / f"{timeframe}.parquet")
    (root / "manifest.json").write_text(json.dumps({
        "dataset_revision": "gold-replay", "generation": "generations/gold-replay",
        "source": {"provider": "dukascopy", "label": "Dukascopy"},
    }), encoding="utf-8")
    store = CandleStore(root)
    service = MarketXRayService(store=store, state=state)
    try:
        service.set_cursor(datetime(2026, 1, 2, 10, tzinfo=UTC))
        payload = service.events()
        assert store.available_symbols == ("XAUUSD",)
        assert payload["events"] == [], (
            "Unscoped previous-store journal row was exposed as current Dukascopy replay: "
            f"{payload}"
        )
        assert payload["total_stored"] == 0
    finally:
        store.close()
        state.close()
