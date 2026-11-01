"""Replaying a moment shows that moment and says where it came from.

Every panel has to match what a store ending at the cursor would have shown, even
after a later moment has been viewed and its zones recorded. The source is named
on the data, because a replay built from one broker must never read as another's,
and a reconstruction must never read as a prediction made at the time.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from market import universe
from market.ingest import ingest_all
from market.orderblocks import ZoneEvent
from market.service import MarketXRayService
from market.state import MarketState
from market.store import CandleStore

SYMBOLS = ("EURUSD", "GBPUSD", "XAUUSD")
START = pd.Timestamp("2026-06-01T00:00:00")
END = pd.Timestamp("2026-10-28T00:00:00")
CURSOR = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)

_TOOL = Path(__file__).resolve().parents[3] / "tools" / "build_replay_store.py"


def _build_tool():
    spec = importlib.util.spec_from_file_location("build_replay_store", _TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bars(symbol: str, minutes: int, until: pd.Timestamp) -> pd.DataFrame:
    index = pd.date_range(START, END, freq=f"{minutes}min", inclusive="left")
    index = index[index + pd.Timedelta(minutes=minutes) <= until]
    rng = np.random.default_rng(sum(map(ord, symbol)))  # fixture only: never served as market data
    steps = rng.normal(0, 0.0015, len(pd.date_range(START, END, freq=f"{minutes}min", inclusive="left")))
    close = 100 * np.exp(np.cumsum(steps))[: len(index)]
    open_ = np.concatenate([[100.0], close[:-1]])
    return pd.DataFrame({
        "ts": index, "open": open_, "close": close,
        "high": np.maximum(open_, close) * 1.0008, "low": np.minimum(open_, close) * 0.9992,
        "spread": 0.01, "volume": 1.0, "m1_bars": minutes,
    })


def _store(root: Path, until: pd.Timestamp = END, manifest: dict | None = None) -> CandleStore:
    for symbol in SYMBOLS:
        (root / symbol).mkdir(parents=True)
        for timeframe, minutes in universe.TIMEFRAMES.items():
            _bars(symbol, minutes, until).to_parquet(root / symbol / f"{timeframe}.parquet")
    if manifest is not None:
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return CandleStore(root)


@pytest.fixture
def service(tmp_path):
    built = MarketXRayService(store=_store(tmp_path / "full"), state=MarketState(tmp_path / "s.db"))
    yield built
    built.store.close()
    built.state.close()


def _event(service, ts: str, timeframe: str = "H1", suffix: str = "a") -> ZoneEvent:
    return ZoneEvent(zone_id="z" + suffix, symbol="XAUUSD", timeframe=timeframe,
                     kind="TAPPED", ts=ts, price=4000.0)


def test_events_from_after_the_cursor_are_not_shown(service):
    service.state.record_events([
        _event(service, "2026-10-27T08:00:00+00:00", suffix="before"),
        _event(service, "2026-10-27T12:00:00+00:00", suffix="after"),
    ])
    service.set_cursor(CURSOR)
    shown = service.events(limit=10)
    assert [e["ts"] for e in shown["events"]] == ["2026-10-27T08:00:00+00:00"]
    assert shown["total_stored"] == 1


def test_an_event_waits_for_its_bar_to_close(service):
    service.state.record_events([_event(service, "2026-10-27T13:00:00+00:00")])
    service.set_cursor(datetime(2026, 10, 27, 13, 30, tzinfo=UTC))
    assert service.events(limit=10)["events"] == []
    service.set_cursor(datetime(2026, 10, 27, 14, 0, tzinfo=UTC))
    assert len(service.events(limit=10)["events"]) == 1


def test_an_event_uses_the_length_of_its_own_timeframe(service):
    service.state.record_events([
        _event(service, "2026-10-27T09:55:00+00:00", "M5", "m5"),
        _event(service, "2026-10-27T09:00:00+00:00", "H4", "h4"),
    ])
    service.set_cursor(CURSOR)
    assert [e["timeframe"] for e in service.events(limit=10)["events"]] == ["M5"]


def test_filtering_by_symbol_still_stops_at_the_cursor(service):
    service.state.record_events([_event(service, "2026-10-27T12:00:00+00:00")])
    service.set_cursor(CURSOR)
    assert service.events(symbol="XAUUSD", limit=10)["events"] == []


def test_rewinding_after_viewing_a_later_moment_hides_what_that_recorded(service):
    service.dashboard(timeframe="H1", symbol="XAUUSD")
    recorded = service.state.recent_events(limit=1000)
    assert recorded, "the fixture produced no zone events, so this proves nothing"
    newest = max(e["ts"] for e in recorded)
    service.set_cursor(datetime.fromisoformat(newest) - timedelta(days=30))
    shown = service.dashboard(timeframe="H1", symbol="XAUUSD")["events"]["events"]
    assert all(e["ts"] < service.cursor.isoformat() for e in shown)


def test_every_panel_matches_a_store_that_ends_at_the_cursor(service, tmp_path):
    ended = pd.Timestamp(CURSOR).tz_localize(None)
    truncated = MarketXRayService(
        store=_store(tmp_path / "ended", ended), state=MarketState(tmp_path / "t.db")
    )
    try:
        service.dashboard(timeframe="H1", symbol="XAUUSD")  # the later moment comes first
        service.set_cursor(CURSOR)
        truncated.set_cursor(CURSOR)
        assert service.cursor == truncated.cursor == CURSOR

        def panels(target):
            payload = target.dashboard(timeframe="H1", symbol="XAUUSD")
            payload["order_blocks"].pop("events_recorded_new")
            shown = {name: payload[name] for name in
                     ("xray", "strength", "leaders", "chart", "order_blocks")}
            shown["events"] = payload["events"]["events"]
            for panel in shown.values():
                if isinstance(panel, dict):
                    panel.pop("provenance", None)
            for event in shown["events"]:
                event.pop("recorded_at")
            return shown

        assert panels(service) == panels(truncated)
    finally:
        truncated.store.close()
        truncated.state.close()


def test_stepping_through_the_window_reveals_one_candle_at_a_time(service):
    service.set_cursor(CURSOR)
    seen = [service.candles(symbol="XAUUSD", timeframe="H1")["candles"][-1]["ts"]]
    for _ in range(4):
        service.step_cursor(timeframe="H1", bars=1)
        seen.append(service.candles(symbol="XAUUSD", timeframe="H1")["candles"][-1]["ts"])
    assert seen == [f"2026-10-27T{hour:02d}:00:00+00:00" for hour in (9, 10, 11, 12, 13)]
    assert service.cursor == datetime(2026, 10, 27, 14, 0, tzinfo=UTC)


# ---- naming the source --------------------------------------------------


SOURCE = {"provider": "dukascopy", "label": "Dukascopy", "caveat": "Dukascopy quotes, not OANDA quotes."}


def test_a_store_names_its_source_on_every_payload(tmp_path):
    served = MarketXRayService(
        store=_store(tmp_path / "named", manifest={"source": SOURCE}), state=MarketState(tmp_path / "s.db")
    )
    try:
        payload = served.dashboard(timeframe="H1", symbol="XAUUSD")
        for panel in (payload, payload["xray"], payload["chart"], payload["events"]):
            assert panel["provenance"]["provider"] == "dukascopy"
            assert panel["provenance"]["source"] == "RECORDED_STORE"
            assert panel["provenance"]["is_live"] is False
    finally:
        served.store.close()
        served.state.close()


def test_a_replay_is_described_as_a_reconstruction_from_that_source(tmp_path):
    served = MarketXRayService(
        store=_store(tmp_path / "named", manifest={"source": SOURCE}), state=MarketState(tmp_path / "s.db")
    )
    try:
        served.set_cursor(CURSOR)
        detail = served.data_provenance().detail
        assert "Reconstructed" in detail and "Dukascopy" in detail
        assert "not a prediction recorded at the time" in detail
        assert "not OANDA" in detail
    finally:
        served.store.close()
        served.state.close()


def test_a_store_with_no_named_source_claims_none(service):
    assert service.data_provenance().provider == "parquet_store"


def test_the_import_records_the_source_it_was_given(tmp_path):
    root = tmp_path / "csv"
    (root / "xauusd_utc").mkdir(parents=True)
    minutes = pd.date_range("2026-10-26T00:00:00", periods=2880, freq="1min")
    for side, shift in (("Bid", 0.0), ("Ask", 0.2)):
        pd.DataFrame({
            "Time (UTC)": minutes.strftime("%Y.%m.%d %H:%M:%S"), "Open": 4000 + shift,
            "High": 4001 + shift, "Low": 3999 + shift, "Close": 4000 + shift, "Volume": 1.0,
        }).to_csv(root / "xauusd_utc" / f"XAUUSD_1 Min_{side}_x.csv", index=False)
    manifest = ingest_all(["XAUUSD"], store_root=tmp_path / "store", start="2026-10-01",
                          csv_root=root, source=SOURCE)
    assert manifest["source"] == SOURCE
    assert CandleStore(tmp_path / "store").manifest["source"] == SOURCE


# ---- what the replay store is built from --------------------------------


def _side(rows):
    return pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close", "Volume"])


def test_closed_market_padding_is_dropped_and_real_minutes_are_kept():
    tool = _build_tool()
    at = pd.date_range("2026-10-27T10:00:00", periods=3, freq="1min", tz="UTC")
    bid = _side([[at[0], 1, 1, 1, 1, 0.0], [at[1], 1, 1, 1, 1, 0.5], [at[2], 1, 2, 1, 2, 0.0]])
    ask = _side([[at[0], 1, 1, 1, 1, 0.0], [at[1], 1, 1, 1, 1, 0.5], [at[2], 1, 2, 1, 2, 0.0]])
    kept, other, dropped = tool.to_minutes(bid, ask)
    assert list(kept["ts"]) == [at[1], at[2]] == list(other["ts"])
    assert dropped == 1


def test_a_minute_quoted_on_one_side_only_is_not_a_bar():
    tool = _build_tool()
    at = pd.date_range("2026-10-27T10:00:00", periods=2, freq="1min", tz="UTC")
    bid = _side([[at[0], 1, 2, 1, 2, 1.0], [at[1], 1, 2, 1, 2, 1.0]])
    ask = _side([[at[0], 1, 2, 1, 2, 1.0]])
    kept, _other, _dropped = tool.to_minutes(bid, ask)
    assert list(kept["ts"]) == [at[0]]


def test_a_gap_in_the_window_is_reported_rather_than_filled():
    tool = _build_tool()
    at = pd.date_range(tool.WINDOW_START, tool.WINDOW_END, freq="1min", inclusive="left")
    keep = at.delete(30)
    rows = _side([[t, 2, 3, 1, 2, 1.0] for t in keep])
    report = tool.check_window("XAUUSD", rows, rows)
    assert report["minutes_expected"] == 240 and report["minutes_present"] == 239
    assert report["missing"] == [at[30].isoformat()]
    assert report["ohlc_invalid"] == 0 and report["crossed_quotes"] == 0


def test_the_tool_never_describes_this_data_as_oanda():
    tool = _build_tool()
    assert tool.SOURCE["provider"] == "dukascopy"
    assert "not OANDA" in tool.SOURCE["caveat"]
