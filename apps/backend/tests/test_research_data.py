from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from research.config import SUBDIRS, ensure_layout, resolve_data_root
from research.fixtures import synthetic_m1
from research.quality import validate_bars
from research.schema import CANONICAL_COLUMNS, derive_timeframe
from research.storage import MarketStore


@pytest.fixture
def bars():
    return synthetic_m1(start="2025-03-03", days=5)


def test_data_root_env_override_and_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("TARS_DATA_ROOT", str(tmp_path / "root"))
    root = ensure_layout()
    assert root == resolve_data_root()
    assert all((root / s).is_dir() for s in SUBDIRS)


def test_data_root_never_defaults_to_system_drive(monkeypatch):
    monkeypatch.delenv("TARS_DATA_ROOT", raising=False)
    root = resolve_data_root()
    assert not str(root).upper().startswith("C:\\TARS_DATA")


def test_canonical_schema(bars):
    assert tuple(bars.columns) == CANONICAL_COLUMNS
    assert str(bars["timestamp_utc"].dt.tz) == "UTC"
    assert (bars["spread"] > 0).all()


def test_parquet_zstd_roundtrip_and_duckdb(tmp_path, bars):
    store = MarketStore(tmp_path)
    files = store.write_bars(bars)
    assert files and files[0].suffix == ".parquet"
    assert pq.ParquetFile(files[0]).metadata.row_group(0).column(0).compression == "ZSTD"
    back = store.read_bars("synthetic_fixture", "EURUSD", "M1")
    pd.testing.assert_frame_equal(back[list(CANONICAL_COLUMNS)].reset_index(drop=True), bars.reset_index(drop=True),
                                  check_dtype=False, check_exact=False)
    # time-bounded read via DuckDB predicate
    sub = store.read_bars("synthetic_fixture", "EURUSD", "M1", start=pd.Timestamp("2025-03-04", tz="UTC"), end=pd.Timestamp("2025-03-05", tz="UTC"))
    assert sub["timestamp_utc"].min() >= pd.Timestamp("2025-03-04", tz="UTC") and sub["timestamp_utc"].max() < pd.Timestamp("2025-03-05", tz="UTC")
    q = store.query("SELECT count(*) AS n FROM read_parquet(?)", [str(files[0])])
    assert int(q["n"][0]) == len(bars)
    assert duckdb.__version__


def test_data_version_is_content_hash(bars):
    v = MarketStore.data_version(bars)
    assert v == MarketStore.data_version(bars.copy())
    changed = bars.copy()
    changed.loc[5, "bid_close"] += 0.0001
    assert MarketStore.data_version(changed) != v


def test_derive_timeframe_deterministic_and_correct(bars):
    m15 = derive_timeframe(bars, "M15")
    assert m15.equals(derive_timeframe(bars, "M15"))
    first = bars[bars["timestamp_utc"] < bars["timestamp_utc"].iloc[0].floor("15min") + pd.Timedelta(minutes=15)]
    row = m15.iloc[0]
    assert row["bid_open"] == first["bid_open"].iloc[0] and row["bid_close"] == first["bid_close"].iloc[-1]
    assert row["bid_high"] == first["bid_high"].max() and row["ask_low"] == first["ask_low"].min()
    assert (m15["source_timeframe"] == "M15").all()
    with pytest.raises(ValueError):
        derive_timeframe(m15, "M1")


def test_clean_data_passes_quality(bars):
    rep = validate_bars(bars)
    assert rep.passed, rep.to_dict()


def _codes(df, **kw):
    return validate_bars(df, **kw).codes("BLOCK")


def test_quality_blocks_duplicates_and_order(bars):
    dup = pd.concat([bars, bars.iloc[[10]]]).sort_values("timestamp_utc", kind="stable").reset_index(drop=True)
    assert "DUPLICATE_TIMESTAMPS" in _codes(dup)
    shuffled = bars.copy()
    shuffled.loc[[20, 21], :] = shuffled.loc[[21, 20], :].values
    assert "OUT_OF_ORDER" in _codes(shuffled)


def test_quality_blocks_invalid_ohlc_and_prices(bars):
    b = bars.copy()
    b.loc[30, "bid_high"] = b.loc[30, "bid_low"] - 0.01
    assert "INVALID_OHLC_BID" in _codes(b)
    z = bars.copy()
    z.loc[40, "ask_close"] = 0.0
    assert "NONPOSITIVE_PRICES" in _codes(z)
    n = bars.copy()
    n.loc[41, "bid_open"] = np.nan
    assert "NULL_PRICES" in _codes(n)


def test_quality_blocks_crossed_and_negative_spread(bars):
    b = bars.copy()
    b.loc[50, "ask_close"] = b.loc[50, "bid_close"] - 0.001
    b.loc[50, "spread"] = -0.001
    codes = _codes(b)
    assert {"BID_ASK_CROSSED", "NEGATIVE_SPREAD"} <= codes


def test_quality_blocks_timezone_and_mixed_provider(bars):
    naive = bars.copy()
    naive["timestamp_utc"] = naive["timestamp_utc"].dt.tz_localize(None)
    assert "TIMEZONE_NOT_UTC" in _codes(naive)
    mixed = bars.copy()
    mixed.loc[100:, "provider"] = "other"
    assert "PROVIDER_DISCONTINUITY" in _codes(mixed)


def test_quality_blocks_missing_bars_and_large_gap(bars):
    gap = bars.drop(index=range(600, 1000)).reset_index(drop=True)  # ~6.7h inside the Monday-Tuesday session
    codes = _codes(gap)
    assert "LARGE_GAPS" in codes and "MISSING_BARS" in codes


def test_weekend_closure_is_not_a_gap_and_weekend_bars_warn(bars):
    rep = validate_bars(bars)
    assert "LARGE_GAPS" not in rep.codes() and "MISSING_BARS" not in rep.codes("BLOCK")
    sat = bars.copy()
    sat.loc[5, "timestamp_utc"] = pd.Timestamp("2025-03-01 12:00", tz="UTC")  # Saturday
    assert "BARS_DURING_MARKET_CLOSURE" in validate_bars(sat).codes("WARN")


def test_quality_abnormal_spread_warns(bars):
    b = bars.copy()
    b.loc[500, ["ask_open", "ask_high", "ask_low", "ask_close"]] += 0.05
    b.loc[500, "spread"] = b.loc[500, "ask_close"] - b.loc[500, "bid_close"]
    assert "ABNORMAL_SPREAD" in validate_bars(b).codes("WARN")


def test_empty_and_missing_columns_block(bars):
    assert "EMPTY_DATASET" in _codes(bars.iloc[0:0])
    assert "SCHEMA_MISSING_COLUMNS" in _codes(bars.drop(columns=["ask_low"]))
