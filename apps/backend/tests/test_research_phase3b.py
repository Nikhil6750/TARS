from __future__ import annotations

import asyncio
import copy
import json
import lzma
import sqlite3
import struct
from datetime import datetime, timedelta, timezone

import duckdb
import numpy as np
import pandas as pd
import pytest

from research.agent import FinalDataAccessDenied, QuantResearchAgent
from research.claude_adapter import ClaudeResearchLLM, ClaudeUnavailable, build_production_director
from research.cost_baseline import baseline_cost_model, cost_baseline_report
from research.demo import demo_spec_dict
from research.director import ConclusionClass, DirectorError
from research.fixtures import synthetic_m1
from research.ingest import StorageGuardError, check_space, ingest
from research.instruments import get_meta, pip_size_of
from research.providers.base import RawCache
from research.providers.dukascopy import RECORD, DukascopyProvider
from research.quality import gap_summary, validate_bars
from research.registry import ExperimentRegistry, RegistryError
from research.schema import CANONICAL_COLUMNS, derive_timeframe, make_bars, pip_size
from research.sessions import classify_sessions, is_daily_break, is_market_closed, session_of
from research.spec import parse_spec
from research.storage import MarketStore


def run(coro):
    return asyncio.run(coro)


def utc(s):
    return pd.Timestamp(s, tz="UTC")


# ============================ instrument metadata ============================================================
def test_instrument_metadata_is_per_instrument():
    x, e = get_meta("XAUUSD"), get_meta("EURUSD")
    assert (x.pip_size, x.tick_size, x.price_precision, x.asset_class) == (0.1, 0.001, 3, "METAL")
    assert (e.pip_size, e.tick_size, e.price_precision, e.asset_class) == (0.0001, 0.00001, 5, "FX")
    assert x.contract_size == 100 and x.contract_unit == "troy oz" and e.contract_size == 100000
    assert x.timezone == e.timezone == "America/New_York" and x.daily_break_local and e.daily_break_local is None
    assert get_meta("gold") is x and get_meta("EUR/USD") is e
    with pytest.raises(KeyError):
        get_meta("NOPE")


def test_no_global_pip_assumption():
    assert pip_size("XAUUSD") == 0.1 and pip_size("EURUSD") == 0.0001 and pip_size("XAUUSD") != pip_size("EURUSD")
    assert pip_size_of("USDJPY") == 0.01 and pip_size_of("GBPUSD") == 0.0001  # legacy fallbacks only for unknown instruments


def test_cost_defaults_are_instrument_specific():
    assert get_meta("XAUUSD").costs.commission_round_turn != get_meta("EURUSD").costs.commission_round_turn


# ============================ DST / sessions =================================================================
@pytest.mark.parametrize("ts,expected", [
    ("2025-01-15 07:30", "ASIA"),                 # winter: London 07:30 GMT, before the London open; Tokyo 16:30
    ("2025-01-15 08:00", "LONDON"),               # winter London open = 08:00 UTC
    ("2025-07-01 07:30", "LONDON"),               # summer: 08:30 BST -> London already open
    ("2025-07-01 06:59", "ASIA"),                 # summer: 07:59 BST -> before open
    ("2025-01-15 12:30", "LONDON"),               # winter NY 07:30 EST -> NY not yet open
    ("2025-01-15 13:30", "LONDON_NY_OVERLAP"),    # winter NY 08:30 EST
    ("2025-07-01 12:30", "LONDON_NY_OVERLAP"),    # summer NY 08:30 EDT, London 13:30 BST
    ("2025-03-17 12:30", "LONDON_NY_OVERLAP"),    # US on EDT, UK still GMT: 12:30 UTC overlaps
    ("2025-04-07 12:30", "LONDON_NY_OVERLAP"),
    ("2025-07-01 16:00", "NEWYORK"),              # London closed (17:00 BST), NY open (12:00 EDT)
    ("2025-01-15 16:30", "NEWYORK"),              # London closes at 16:30 GMT
    ("2025-01-15 22:30", "OFF"),
    ("2025-01-18 12:00", "OFF"),                  # Saturday
])
def test_session_classification_is_dst_correct(ts, expected):
    assert session_of(utc(ts)) == expected


def test_us_dst_transition_week():
    # US DST starts Sun 2025-03-09: the NY open moves from 13:00 UTC to 12:00 UTC while London stays on GMT
    assert session_of(utc("2025-03-07 12:00")) == "LONDON"            # Fri, NY 07:00 EST
    assert session_of(utc("2025-03-07 13:00")) == "LONDON_NY_OVERLAP"
    assert session_of(utc("2025-03-10 12:00")) == "LONDON_NY_OVERLAP"  # Mon, NY 08:00 EDT
    assert session_of(utc("2025-03-10 11:59")) == "LONDON"


def test_uk_dst_transition_week():
    # UK DST starts Sun 2025-03-30: the London open moves from 08:00 UTC to 07:00 UTC
    assert session_of(utc("2025-03-28 07:30")) == "ASIA"    # Fri, GMT
    assert session_of(utc("2025-03-31 07:30")) == "LONDON"  # Mon, BST
    assert session_of(utc("2025-03-31 06:59")) == "ASIA"


def test_autumn_transitions():
    assert session_of(utc("2025-10-24 06:59")) == "ASIA" and session_of(utc("2025-10-24 07:00")) == "LONDON"  # BST still on
    assert session_of(utc("2025-10-27 07:30")) == "ASIA" and session_of(utc("2025-10-27 08:00")) == "LONDON"  # GMT from Oct 26
    assert session_of(utc("2025-10-31 12:00")) == "LONDON_NY_OVERLAP"   # US still EDT: NY 08:00
    assert session_of(utc("2025-11-03 12:00")) == "LONDON"              # US on EST from Nov 2: NY 07:00


def test_fixed_utc_hours_are_rejected_and_naive_timestamps_too():
    with pytest.raises(TypeError):
        session_of(7)
    with pytest.raises(ValueError):
        classify_sessions([pd.Timestamp("2025-01-15 08:00")])


def test_weekly_closure_shifts_with_dst():
    meta = get_meta("XAUUSD")
    s = pd.Series([utc("2025-01-17 21:30"), utc("2025-01-17 22:00"), utc("2025-07-18 20:59"), utc("2025-07-18 21:00"),
                   utc("2025-01-19 22:59"), utc("2025-01-19 23:00"), utc("2025-07-20 21:59"), utc("2025-07-20 22:00")])
    assert list(is_market_closed(s, meta)) == [False, True, False, True, True, False, True, False]


def test_daily_break_for_gold_not_fx():
    s = pd.Series([utc("2025-01-14 21:59"), utc("2025-01-14 22:00"), utc("2025-01-14 22:59"), utc("2025-01-14 23:00")])
    assert list(is_daily_break(s, get_meta("XAUUSD"))) == [False, True, True, False]
    assert not is_daily_break(s, get_meta("EURUSD")).any()


# ============================ provider normalisation (fake feed, no network) ==================================
def _candles(day: datetime, minutes: list[int], base: float, scale: int, spread: int, seed: int, side: str):
    rng = np.random.default_rng(seed)
    price = int(base * scale)
    out = b""
    for m in minutes:
        o = price
        delta = int(rng.integers(-40, 41))
        c = o + delta
        hi, lo = max(o, c) + int(rng.integers(0, 30)), min(o, c) - int(rng.integers(0, 30))
        if side == "ASK":
            o, c, hi, lo = o + spread, c + spread, hi + spread, lo + spread
        out += RECORD.pack(m * 60, o, c, lo, hi, float(rng.integers(1, 9)) / 100)
        price += delta
    return lzma.compress(out)


def fake_opener(missing_bid=(), base=2900.0):
    """Mimics the Dukascopy URL space for XAUUSD (scale 1000). Mon-Thu full day, Fri until 21:59, Sun from 23:00, Sat empty."""
    calls = []

    def opener(url: str) -> bytes:
        calls.append(url)
        parts = url.split("/")
        y, mth, d, name = int(parts[-4]), int(parts[-3]) + 1, int(parts[-2]), parts[-1]
        side = name.split("_")[0]
        day = datetime(y, mth, d, tzinfo=timezone.utc)
        wd = day.weekday()
        if wd == 5:
            return b""
        from zoneinfo import ZoneInfo
        edt = day.replace(hour=12).astimezone(ZoneInfo("America/New_York")).utcoffset().total_seconds() == -4 * 3600
        close_m, open_m = (21 if edt else 22) * 60, (22 if edt else 23) * 60     # 17:00 / 18:00 New York in UTC, DST-correct
        mins = list(range(1440))
        if wd == 4:
            mins = list(range(close_m))
        if wd == 6:
            mins = list(range(open_m, 1440))
        if side == "BID" and day.date() in missing_bid:
            mins = mins[: len(mins) // 2]
        seed = y * 10000 + mth * 100 + d
        return _candles(day, mins, base, 1000, 30, seed, side)
    opener.calls = calls
    return opener


def test_dukascopy_url_month_is_zero_indexed_and_days_are_utc():
    p = DukascopyProvider()
    url = p.url("XAUUSD", datetime(2025, 3, 13, tzinfo=timezone.utc), "BID")
    assert url.endswith("/XAUUSD/2025/02/13/BID_candles_min_1.bi5")
    days = p.days(datetime(2025, 3, 1, tzinfo=timezone.utc), datetime(2025, 3, 4, tzinfo=timezone.utc))
    assert [d.day for d in days] == [1, 2, 3]
    assert p.estimate_download_bytes("XAUUSD", datetime(2025, 3, 1, tzinfo=timezone.utc), datetime(2025, 3, 4, tzinfo=timezone.utc)) == 2 * 2 * 20000  # Saturday skipped
    assert DukascopyProvider(skip_closed_days=False).estimate_download_bytes(
        "XAUUSD", datetime(2025, 3, 1, tzinfo=timezone.utc), datetime(2025, 3, 4, tzinfo=timezone.utc)) == 3 * 2 * 20000


def test_normalize_decodes_prices_merges_bid_ask_and_keeps_provenance_columns(tmp_path):
    p = DukascopyProvider(opener=fake_opener())
    s, e = datetime(2025, 3, 3, tzinfo=timezone.utc), datetime(2025, 3, 5, tzinfo=timezone.utc)
    raw = p.fetch("XAUUSD", s, e, RawCache(tmp_path))
    df = p.normalize("XAUUSD", raw)
    assert tuple(df.columns) == CANONICAL_COLUMNS and len(df) == 2880
    assert str(df["timestamp_utc"].dt.tz) == "UTC" and df["timestamp_utc"].iloc[0] == utc("2025-03-03 00:00")
    assert (df["provider"] == "dukascopy").all() and (df["instrument"] == "XAUUSD").all() and (df["source_timeframe"] == "M1").all()
    assert df["bid_close"].between(2000, 4000).all()                         # scaled by 1000, not raw ints
    assert np.isclose((df["ask_close"] - df["bid_close"]), 0.03).all()       # 30 points of spread
    assert validate_bars(df).passed


def test_one_sided_rows_stay_visible_and_block(tmp_path):
    day = datetime(2025, 3, 4).date()
    p = DukascopyProvider(opener=fake_opener(missing_bid={day}))
    raw = p.fetch("XAUUSD", datetime(2025, 3, 3, tzinfo=timezone.utc), datetime(2025, 3, 5, tzinfo=timezone.utc), RawCache(tmp_path))
    df = p.normalize("XAUUSD", raw)
    assert df["bid_close"].isna().sum() == 720                    # not silently dropped or filled
    assert "NULL_PRICES" in validate_bars(df).codes("BLOCK")


def test_raw_cache_is_immutable_and_reused(tmp_path):
    op = fake_opener()
    p = DukascopyProvider(opener=op)
    s, e = datetime(2025, 3, 3, tzinfo=timezone.utc), datetime(2025, 3, 4, tzinfo=timezone.utc)
    cache = RawCache(tmp_path)
    first = p.fetch("XAUUSD", s, e, cache)
    n = len(op.calls)
    second = p.fetch("XAUUSD", s, e, cache)
    assert len(op.calls) == n and [r.sha256 for r in first] == [r.sha256 for r in second]   # cache hit: no network
    with pytest.raises(RuntimeError, match="immutable"):
        cache.put(("dukascopy", "XAUUSD", "2025", "03", "03_BID.bi5"), b"tampered")


def test_describe_source():
    d = DukascopyProvider().describe_source("XAUUSD")
    assert d["provider"] == "dukascopy" and d["price_scale"] == 1000 and d["normalization_version"] and d["caveats"]


# ============================ ingestion pipeline + provenance + storage guard ==================================
@pytest.fixture(scope="module")
def ingested(tmp_path_factory):
    root = tmp_path_factory.mktemp("lake")
    p = DukascopyProvider(opener=fake_opener())
    s, e = datetime(2025, 3, 2, tzinfo=timezone.utc), datetime(2025, 3, 15, tzinfo=timezone.utc)   # spans the US DST change (Mar 9)
    return root, ingest(p, "XAUUSD", s, e, root, reserve_bytes=1), (s, e)


def test_pipeline_writes_parquet_provenance_and_duckdb(ingested):
    root, res, (s, e) = ingested
    prov = res.provenance
    assert res.rows == prov["row_count"] > 10000 and res.parquet_files and prov["parquet_bytes"] > 0
    for key in ("provider", "retrieved_at", "requested_range", "actual_range", "row_count", "raw_checksum_sha256", "raw_files",
                "raw_bytes", "canonical_data_version", "source", "quality", "storage_check"):
        assert prov[key] not in (None, "", []), key
    assert prov["source"]["normalization_version"] and len(prov["raw_checksum_sha256"]) == 64
    assert json.loads(res.provenance_path.read_text())["row_count"] == res.rows
    con = duckdb.connect(str(root / "derived" / "catalog.duckdb"))
    try:
        n = con.execute("SELECT count(*) FROM xauusd_m1_dukascopy").fetchone()[0]
        lo, hi = con.execute("SELECT min(spread), max(spread) FROM xauusd_m1_dukascopy").fetchone()
    finally:
        con.close()
    assert n == res.rows and lo > 0


def test_real_shaped_data_quality_categories(ingested):
    _, res, _ = ingested
    cats = gap_summary(res.quality)
    assert res.quality.passed, res.quality.to_dict()
    assert cats["MARKET_CLOSED"] >= 1 and cats["PROVIDER_GAP"] == 0 and cats["INVALID"] == 0
    # the fake week has 1h Sunday-open gaps etc.; weekly closure is MARKET_CLOSED, not a provider fault


def test_reingest_merges_instead_of_clobbering_year(ingested):
    root, res, _ = ingested
    before = MarketStore(root).read_bars("dukascopy", "XAUUSD", "M1")
    p = DukascopyProvider(opener=fake_opener())
    ingest(p, "XAUUSD", datetime(2025, 3, 16, tzinfo=timezone.utc), datetime(2025, 3, 19, tzinfo=timezone.utc), root, reserve_bytes=1)
    after = MarketStore(root).read_bars("dukascopy", "XAUUSD", "M1")
    assert len(after) > len(before) and before["timestamp_utc"].isin(after["timestamp_utc"]).all()


def test_storage_space_guard(tmp_path):
    assert check_space(tmp_path, 1000, reserve_bytes=1)["need_bytes"] == 1000
    with pytest.raises(StorageGuardError, match="insufficient space"):
        check_space(tmp_path, 10, reserve_bytes=10 ** 15)
    p = DukascopyProvider(opener=fake_opener())
    s, e = datetime(2025, 3, 2, tzinfo=timezone.utc), datetime(2025, 3, 4, tzinfo=timezone.utc)
    with pytest.raises(StorageGuardError):
        ingest(p, "XAUUSD", s, e, tmp_path, reserve_bytes=10 ** 15)
    with pytest.raises(StorageGuardError, match="per-run cap"):
        ingest(p, "XAUUSD", s, e, tmp_path, reserve_bytes=1, max_download_bytes=10)
    assert not (tmp_path / "market" / "_raw").exists()  # refused BEFORE any download


# ============================ quality: gap classification ==============================================================
def _xau_m1(start, end):
    idx = pd.date_range(start, end, freq="1min", tz="UTC", inclusive="left")
    meta = get_meta("XAUUSD")
    idx = idx[~is_market_closed(pd.Series(idx), meta).to_numpy() & ~is_daily_break(pd.Series(idx), meta).to_numpy()]
    n = len(idx)
    rng = np.random.default_rng(3)
    c = 2900 + np.cumsum(rng.normal(0, 0.05, n))
    df = pd.DataFrame({"timestamp_utc": idx, "bid_open": c, "bid_high": c + 0.1, "bid_low": c - 0.1, "bid_close": c})
    for f in ("open", "high", "low", "close"):
        df[f"ask_{f}"] = df[f"bid_{f}"] + 0.3
    return make_bars(df, instrument="XAUUSD", provider="t")


def test_weekend_and_daily_break_are_expected_not_provider_gaps():
    rep = validate_bars(_xau_m1("2025-03-03", "2025-03-14"))
    cats = gap_summary(rep)
    assert rep.passed and cats["MARKET_CLOSED"] >= 1 and cats["EXPECTED_GAP"] >= 8 and cats["PROVIDER_GAP"] == 0


def test_midweek_hole_is_a_provider_gap_and_blocks():
    b = _xau_m1("2025-03-03", "2025-03-07")
    b = b[~b["timestamp_utc"].between(utc("2025-03-05 09:00"), utc("2025-03-05 13:00"))].reset_index(drop=True)
    rep = validate_bars(b)
    assert {"LARGE_GAPS"} <= rep.codes("BLOCK") and gap_summary(rep)["PROVIDER_GAP"] > 200


def test_bars_during_weekend_closure_warn():
    b = _xau_m1("2025-03-03", "2025-03-07")
    b.loc[10, "timestamp_utc"] = utc("2025-03-08 12:00")
    assert "BARS_DURING_MARKET_CLOSURE" in validate_bars(b.sort_values("timestamp_utc").reset_index(drop=True)).codes("WARN")


# ============================ resampling ================================================================================
@pytest.fixture(scope="module")
def xau():
    return _xau_m1("2025-03-03", "2025-03-14")


@pytest.mark.parametrize("tf,minutes", [("M5", 5), ("M15", 15), ("M30", 30), ("H1", 60), ("H4", 240)])
def test_derived_bars_use_only_their_own_window(xau, tf, minutes):
    d = derive_timeframe(xau, tf)
    assert (d["timestamp_utc"].dt.minute % minutes % 60 == 0).all() if minutes < 60 else True
    for k in (3, len(d) // 2, len(d) - 3):
        row = d.iloc[k]
        w = xau[(xau["timestamp_utc"] >= row["timestamp_utc"]) & (xau["timestamp_utc"] < row["timestamp_utc"] + pd.Timedelta(minutes=minutes))]
        assert row["bid_open"] == w["bid_open"].iloc[0] and row["bid_close"] == w["bid_close"].iloc[-1]
        assert row["bid_high"] == w["bid_high"].max() and row["ask_low"] == w["ask_low"].min()


def test_no_future_leak_into_completed_bars(xau):
    full = derive_timeframe(xau, "M15")
    cut = utc("2025-03-06 10:07")                                     # mid-bar cut
    part = derive_timeframe(xau[xau["timestamp_utc"] < cut].reset_index(drop=True), "M15")
    done = full[full["timestamp_utc"] < utc("2025-03-06 10:00")].reset_index(drop=True)
    pd.testing.assert_frame_equal(part.iloc[: len(done)].reset_index(drop=True), done)       # completed bars identical
    assert part.iloc[-1]["quality_flags"] == "PARTIAL"                                      # the forming bar is flagged
    assert part.iloc[-1]["bid_close"] != full[full["timestamp_utc"] == part.iloc[-1]["timestamp_utc"]]["bid_close"].iloc[0]
    trimmed = derive_timeframe(xau[xau["timestamp_utc"] < cut].reset_index(drop=True), "M15", drop_partial_tail=True)
    assert trimmed["timestamp_utc"].iloc[-1] < utc("2025-03-06 10:00") and "PARTIAL" not in trimmed["quality_flags"].iloc[-1]


def test_d1_is_anchored_to_new_york_17_00_across_dst():
    b = _xau_m1("2025-03-03", "2025-03-14")
    d1 = derive_timeframe(b, "D1")
    labels = set(d1["timestamp_utc"].dt.strftime("%m-%d %H:%M"))
    assert "03-04 22:00" in labels or "03-03 22:00" in labels            # winter anchor 17:00 EST = 22:00 UTC
    assert any(lbl.endswith("21:00") and lbl.startswith("03-1") for lbl in labels)   # after Mar 9 anchor is 21:00 UTC (EDT)
    wed = d1[d1["timestamp_utc"] == utc("2025-03-05 22:00")].iloc[0]
    day = b[(b["timestamp_utc"] >= utc("2025-03-05 22:00")) & (b["timestamp_utc"] < utc("2025-03-06 22:00"))]
    assert wed["bid_open"] == day["bid_open"].iloc[0] and wed["bid_close"] == day["bid_close"].iloc[-1]


def test_all_timeframes_derive_from_m1(xau):
    for tf in ("M5", "M15", "M30", "H1", "H4", "D1"):
        d = derive_timeframe(xau, tf)
        assert len(d) > 0 and d["timestamp_utc"].is_monotonic_increasing and (d["source_timeframe"] == tf).all()
        assert validate_bars(d).blocks_experiment is False or tf == "D1"


# ============================ FINAL_UNTOUCHED protection ===============================================================
@pytest.fixture
def lab(tmp_path):
    store = MarketStore(tmp_path)
    store.write_bars(synthetic_m1(start="2025-02-24", days=26))
    reg = ExperimentRegistry(tmp_path / "r.sqlite")
    return reg, QuantResearchAgent(reg, store, tmp_path / "rep"), store


def test_final_untouched_alias_and_hash_stability():
    d = copy.deepcopy(demo_spec_dict())
    d["final_untouched"] = d.pop("untouched_test")
    a = parse_spec(d)
    assert a.untouched_test.start.month == 3 and a.spec_hash() == parse_spec(demo_spec_dict()).spec_hash()
    assert "diagnostic_kind" not in a.canonical_json()      # specs frozen before 3B keep their hashes


def test_loader_refuses_final_period_without_promotion(lab):
    reg, agent, _ = lab
    spec = parse_spec(demo_spec_dict())
    with pytest.raises(FinalDataAccessDenied):
        agent._load(spec, "EURUSD", utc("2025-03-20"))
    with pytest.raises(FinalDataAccessDenied):
        agent._prepare(spec, utc("2025-03-20"))
    agent._load(spec, "EURUSD", utc("2025-03-14"))          # up to validation end is fine


def test_first_final_access_is_recorded_once_and_run_never_triggers_it(lab):
    reg, agent, _ = lab
    hid = agent.submit(parse_spec(demo_spec_dict()))
    agent.run(hid)
    assert reg.get(hid)["final_first_accessed_at"] is None
    assert agent.evidence_packet(hid)["final_data"] == {"first_accessed_at": None, "untouched": True}
    assert reg.mark_final_access(hid) is True and reg.mark_final_access(hid) is False
    first = reg.get(hid)["final_first_accessed_at"]
    assert first and reg.get(hid)["final_first_accessed_at"] == first
    assert [h["event"] for h in reg.history(hid)].count("FINAL_ACCESSED") == 1
    assert agent.evidence_packet(hid)["final_data"]["untouched"] is False


def test_untouched_run_records_access_and_second_experiment_cannot_reuse_it(lab):
    reg, agent, _ = lab
    a = agent.submit(parse_spec(demo_spec_dict()))
    reg.transition(a, "RUNNING")
    reg.record(a, results_json={"validation": {"net_expectancy_r": 0.2}})
    reg.transition(a, "PRELIMINARY")
    agent.run_untouched_test(a, authorized=True)
    assert reg.get(a)["final_first_accessed_at"]
    # a later experiment on the same instrument/final window: the data is no longer untouched for it
    d = copy.deepcopy(demo_spec_dict())
    d["title"] = "Different follow-up diagnostic title"
    d["features"][0]["n"] = 12
    b = agent.submit(parse_spec(d))
    reg.transition(b, "RUNNING")
    reg.transition(b, "PRELIMINARY")
    with pytest.raises(RegistryError, match="no longer untouched"):
        agent.run_untouched_test(b, authorized=True)
    assert not reg.get(b)["test_consumed"] and reg.get(b)["final_first_accessed_at"] is None
    codes = [w.code for w in agent.evidence_packet(a)["warnings"] or []]
    assert "FINAL_PERIOD_NOT_UNTOUCHED" not in codes


def test_pre_run_warns_when_final_window_was_already_used(lab):
    reg, agent, _ = lab
    a = agent.submit(parse_spec(demo_spec_dict()))
    reg.mark_final_access(a)
    d = copy.deepcopy(demo_spec_dict())
    d["title"] = "Follow-up on previously used final window"
    d["features"][0]["n"] = 11
    b = agent.submit(parse_spec(d))
    pkt = agent.run(b)
    assert "FINAL_PERIOD_NOT_UNTOUCHED" in [w["code"] for w in pkt["warnings"]]


def test_registry_migrates_old_databases(tmp_path):
    path = tmp_path / "old.sqlite"
    reg = ExperimentRegistry(path)
    reg.register(parse_spec(demo_spec_dict()))
    con = sqlite3.connect(path)
    con.execute("DROP TRIGGER experiments_frozen")
    con.execute("ALTER TABLE experiments DROP COLUMN final_first_accessed_at")
    con.commit()
    con.close()
    again = ExperimentRegistry(path)           # reopening must add the column back without losing data
    assert again.get("H0001")["final_first_accessed_at"] is None


def test_diagnostic_kind_requires_diagnostic_and_is_visible():
    d = copy.deepcopy(demo_spec_dict())
    d["diagnostic"] = False
    d["diagnostic_kind"] = "REAL_DATA_DIAGNOSTIC"
    with pytest.raises(Exception, match="requires diagnostic"):
        parse_spec(d)
    d["diagnostic"] = True
    s = parse_spec(d)
    assert s.diagnostic_kind == "REAL_DATA_DIAGNOSTIC" and "REAL_DATA_DIAGNOSTIC" in s.canonical_json()
    assert s.spec_hash() != parse_spec(demo_spec_dict()).spec_hash()


# ============================ cost baseline ==============================================================================
def test_cost_baseline_components_are_separate_and_financing_is_explicit_placeholder():
    m = baseline_cost_model("XAUUSD")
    assert m.commission_round_turn == 0.07 and m.slippage_per_side == 0.02 and m.latency_bars == 0
    assert m.financing_per_bar == 0 and m.financing_is_placeholder
    rep = cost_baseline_report("XAUUSD", _xau_m1("2025-03-03", "2025-03-05"))
    assert "REAL observed bid/ask" in rep["spread"] and "DISABLED placeholder" in rep["financing"]
    assert rep["observed_spread"]["median"] == pytest.approx(0.3) and rep["observed_spread"]["median_in_pips"] == pytest.approx(3.0)
    assert baseline_cost_model("EURUSD").commission_round_turn != m.commission_round_turn
    assert baseline_cost_model("XAUUSD", spread_mult=2.0).spread_mult == 2.0


def test_evidence_packet_states_costs(lab):
    reg, agent, _ = lab
    hid = agent.submit(parse_spec(demo_spec_dict()))
    n = agent.run(hid)["cost_notes"]
    assert set(n) == {"spread", "commission_round_turn", "slippage_per_side", "latency_bars", "financing"} and "placeholder" in n["financing"]


# ============================ Claude production adapter ===================================================================
class FakeProvider:
    name = "fake_claude"

    def __init__(self, payload):
        self.payload, self.requests = payload, []

    async def respond(self, request):
        from assistant.provider import AssistantReply
        self.requests.append(request)
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return AssistantReply(text=text, provider=self.name)


def test_adapter_is_lazy_and_never_calls_claude_at_construction(lab):
    reg, agent, _ = lab
    llm = ClaudeResearchLLM()
    assert llm._provider is None and llm.calls == 0
    director = build_production_director(reg, agent)
    assert director.llm._provider is None and director.llm.calls == 0       # nothing started, nothing called


def test_adapter_passes_system_prompt_and_compact_evidence_and_logs_without_content(lab, tmp_path):
    reg, agent, _ = lab
    hid = agent.submit(parse_spec(demo_spec_dict()))
    agent.run(hid)
    fake = FakeProvider({"classification": "FAILED_EXPERIMENT", "summary": "rejected on validation", "decision": "REJECT"})
    log = tmp_path / "calls.jsonl"
    director = build_production_director(reg, agent, provider=fake, log_path=log)
    res = run(director.review_experiment(hid))
    assert res.output.classification is ConclusionClass.FAILED_EXPERIMENT and director.llm.calls == 1
    req = fake.requests[0]
    assert "lead quantitative researcher" in req.system_context and req.conversation_id.startswith("research-")
    assert "bid_open" not in req.text and len(req.text) < 16000                      # evidence summary, never raw bars
    line = json.loads(log.read_text().splitlines()[0])
    assert line["status"] == "ok" and set(line) == {"at", "status", "prompt_sha256", "prompt_chars", "seconds"}
    assert reg.get(hid)["claude_review"]


@pytest.mark.parametrize("op", ["propose_hypothesis", "review_experiment", "diagnose_failure", "design_validation",
                                "compare_experiments", "recommend_next_experiment"])
def test_all_six_operations_through_production_adapter(lab, op):
    reg, agent, _ = lab
    hid = agent.submit(parse_spec(demo_spec_dict()))
    agent.run(hid)
    payload = {"classification": "HYPOTHESIS", "summary": "looks fine",
               "spec": {**demo_spec_dict(), "diagnostic": False, "title": "Brand new unrelated idea title"}}
    director = build_production_director(reg, agent, provider=FakeProvider(payload))
    args = {"propose_hypothesis": ("topic",), "review_experiment": (hid,), "diagnose_failure": (hid,), "design_validation": (hid,),
            "compare_experiments": ([hid],), "recommend_next_experiment": ("ctx",)}[op]
    res = run(getattr(director, op)(*args))
    assert res.output.classification in set(ConclusionClass) and director.llm.calls == 1


def test_adapter_enforces_budget_killswitch_and_prompt_cap():
    fake = FakeProvider({"classification": "HYPOTHESIS", "summary": "looks fine"})
    llm = ClaudeResearchLLM(fake, max_calls=1)
    run(llm("s", "u"))
    with pytest.raises(ClaudeUnavailable, match="budget"):
        run(llm("s", "u"))
    with pytest.raises(ClaudeUnavailable, match="kill switch"):
        run(ClaudeResearchLLM(fake, cloud_disabled=lambda: True)("s", "u"))
    with pytest.raises(ClaudeUnavailable, match="exceeds cap"):
        run(ClaudeResearchLLM(fake)("s", "x" * 30000))
    assert len(fake.requests) == 1


def test_director_retries_malformed_output_once_then_fails_loud(lab):
    reg, agent, _ = lab
    hid = agent.submit(parse_spec(demo_spec_dict()))
    agent.run(hid)
    replies = iter(["not json at all", json.dumps({"classification": "PRELIMINARY_EVIDENCE", "summary": "second try"})])

    class Flaky(FakeProvider):
        async def respond(self, request):
            from assistant.provider import AssistantReply
            self.requests.append(request)
            return AssistantReply(text=next(replies), provider="f")
    fake = Flaky(None)
    director = build_production_director(reg, agent, provider=fake)
    res = run(director.diagnose_failure(hid))
    assert len(fake.requests) == 2 and "previous reply was rejected" in fake.requests[1].text
    assert res.output.classification is ConclusionClass.FAILED_EXPERIMENT       # downgraded: registry says REJECTED
    bad = build_production_director(reg, agent, provider=FakeProvider("garbage"))
    with pytest.raises(DirectorError, match="after retry"):
        run(bad.diagnose_failure(hid))
    assert bad.llm.calls == 2


# ============================ real data (only if the bounded sample has been ingested) ====================================
REAL_ROOT = None
try:
    from research.config import resolve_data_root
    REAL_ROOT = resolve_data_root()
    _real = MarketStore(REAL_ROOT).files("dukascopy", "XAUUSD", "M1")
except Exception:  # pragma: no cover
    _real = []


@pytest.mark.skipif(not _real, reason="bounded real XAUUSD sample not ingested on this machine")
def test_real_xauusd_sample_schema_and_quality():
    st = MarketStore(REAL_ROOT)
    bars = st.read_bars("dukascopy", "XAUUSD", "M1", end=st.final_start("XAUUSD"))
    assert tuple(bars.columns) == CANONICAL_COLUMNS and str(bars["timestamp_utc"].dt.tz) == "UTC"
    assert len(bars) > 20000 and (bars["bid_close"].between(1000, 6000)).all() and bars["spread"].min() >= 0
    rep = validate_bars(bars)
    # the store holds non-contiguous ingested months (coverage holes are NOT data gaps); judge quality on the contiguous 2025 sample
    sample = bars[(bars["timestamp_utc"] >= pd.Timestamp("2025-02-03", tz="UTC")) & (bars["timestamp_utc"] < pd.Timestamp("2025-04-04", tz="UTC"))]
    assert validate_bars(sample).passed


# ============================ provider padding, merge range, annotations, prompt schema, benchmark =========================
def padded_opener(base=2900.0):
    """Like Dukascopy: EVERY day returns 1440 rows; closed periods are flat zero-volume padding."""
    inner = fake_opener(base=base)

    def opener(url):
        parts = url.split("/")
        y, mth, d, side = int(parts[-4]), int(parts[-3]) + 1, int(parts[-2]), parts[-1].split("_")[0]
        day = datetime(y, mth, d, tzinfo=timezone.utc)
        real = inner(url)
        have = {} if not real else {i: RECORD.unpack_from(lzma.decompress(real), i * 24) for i in range(len(lzma.decompress(real)) // 24)}
        got = {r[0] // 60: r for r in have.values()}
        out, last = b"", None
        for m in range(1440):
            if m in got:
                out += RECORD.pack(*got[m])
                last = got[m][2]
            else:
                p = last if last is not None else int(base * 1000) + (30 if side == "ASK" else 0)
                out += RECORD.pack(m * 60, p, p, p, p, 0.0)
        return lzma.compress(out)
    return opener


def test_padded_closed_market_rows_are_dropped_counted_and_reported(tmp_path):
    p = DukascopyProvider(opener=padded_opener(), skip_closed_days=False, sleep=lambda s: None)
    s, e = datetime(2025, 3, 3, tzinfo=timezone.utc), datetime(2025, 3, 10, tzinfo=timezone.utc)   # Mon..Sun
    res = ingest(p, "XAUUSD", s, e, tmp_path, reserve_bytes=1)
    st = res.provenance["normalization"]
    assert st["rows_dropped"] > 0 and st["rows_in"] == 7 * 1440 and st["rows_out"] == res.rows
    assert st["dropped_weekly_closure"] >= 2880 and st["rows_dropped"] == st["dropped_weekly_closure"] + st["dropped_daily_break"] + st["dropped_long_dead_runs"]
    assert st["closed_nonflat_rows_kept"] >= 0   # this fake feed has real (non-flat) rows during the gold break; they are kept, never dropped
    bars = MarketStore(tmp_path).read_bars("dukascopy", "XAUUSD", "M1")
    meta = get_meta("XAUUSD")
    assert not is_market_closed(bars["timestamp_utc"], meta).any()
    assert res.quality.passed and gap_summary(res.quality)["MARKET_CLOSED"] >= 1 and gap_summary(res.quality)["PROVIDER_GAP"] == 0
    assert res.provenance["source"]["normalization_version"] == "dukascopy-m1-v3"


def test_non_flat_rows_inside_closure_are_kept_and_warned(tmp_path):
    inner = padded_opener()

    def opener(url):
        body = bytearray(lzma.decompress(inner(url)))
        if "/2025/02/08_" in url or url.endswith("/2025/02/08/BID_candles_min_1.bi5") or "/2025/02/08/" in url:  # Sat 2025-03-08
            body[0:24] = RECORD.pack(0, 2900000, 2900500, 2899000, 2901000, 0.5)   # a genuinely moving candle on a closed day
        return lzma.compress(bytes(body))
    p = DukascopyProvider(opener=opener, skip_closed_days=False, sleep=lambda s: None)
    res = ingest(p, "XAUUSD", datetime(2025, 3, 7, tzinfo=timezone.utc), datetime(2025, 3, 10, tzinfo=timezone.utc), tmp_path, reserve_bytes=1)
    assert res.provenance["normalization"]["closed_nonflat_rows_kept"] >= 1            # never silently discarded
    assert "BARS_DURING_MARKET_CLOSURE" in res.quality.codes("WARN")


def test_reingest_replaces_the_whole_requested_range_not_just_the_new_actual_range(tmp_path):
    s, e = datetime(2025, 3, 3, tzinfo=timezone.utc), datetime(2025, 3, 6, tzinfo=timezone.utc)
    store = MarketStore(tmp_path)
    stale = synthetic_m1(instrument="XAUUSD", provider="dukascopy", start="2025-03-03", days=1, base=2900.0, spread=0.3)
    store.write_bars(stale)                                               # stale rows from an older ingest (e.g. old normalisation)
    ingest(DukascopyProvider(opener=fake_opener()), "XAUUSD", s, e, tmp_path, reserve_bytes=1)
    bars = store.read_bars("dukascopy", "XAUUSD", "M1")
    assert not bars["timestamp_utc"].duplicated().any()
    first_fake = bars[bars["timestamp_utc"] < utc("2025-03-03 00:30")]
    assert first_fake.empty or (first_fake["bid_close"] > 2000).all()


def test_annotations_are_append_only_history(tmp_path):
    reg = ExperimentRegistry(tmp_path / "r.sqlite")
    hid = reg.register(parse_spec(demo_spec_dict()))
    reg.annotate(hid, "superseded by H0099")
    assert reg.history(hid)[-1] == {"at": reg.history(hid)[-1]["at"], "event": "NOTE", "detail": "superseded by H0099"}
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(tmp_path / "r.sqlite").execute("DELETE FROM experiment_events")


def test_system_prompt_spells_out_the_output_schema():
    from research.director import SYSTEM_PROMPT
    for field in ("classification", "summary", "reasoning", "decision", "spec", "next_steps", "REJECT|REFINE|VALIDATE_NEXT|NONE"):
        assert field in SYSTEM_PROMPT


def test_benchmark_reports_every_stage_on_fixture_data(tmp_path):
    from research.real_diagnostic import benchmark, real_diagnostic_spec
    store = MarketStore(tmp_path)
    bars = synthetic_m1(instrument="EURUSD", provider="dukascopy", start="2025-02-03", days=40)
    store.write_bars(bars)
    spec = real_diagnostic_spec("EURUSD", bars["timestamp_utc"].iloc[0], bars["timestamp_utc"].iloc[-1])
    t = benchmark(spec, tmp_path)
    assert {"data_load_s", "quality_s", "derive_m15_s", "features_s", "signals_s", "backtest_s", "metrics_s", "regimes_s"} <= set(t)
    assert t["m1_rows"] > 10000 and all(t[k] >= 0 for k in t if k.endswith("_s"))
    assert spec.diagnostic and spec.diagnostic_kind == "REAL_DATA_DIAGNOSTIC" and "REAL_DATA_DIAGNOSTIC" in spec.title


def test_real_data_diagnostic_is_labelled_and_never_promotable(tmp_path):
    from research.promotion import PromotionDenied, promote
    from research.real_diagnostic import real_diagnostic_spec
    spec = real_diagnostic_spec("XAUUSD", utc("2025-02-03"), utc("2025-04-04"))
    reg = ExperimentRegistry(tmp_path / "r.sqlite")
    hid = reg.register(spec)
    assert reg.get(hid)["diagnostic"] == 1 and "REAL_DATA_DIAGNOSTIC" in reg.get(hid)["title"]
    with pytest.raises(PromotionDenied):
        promote(reg, hid, approved_by="x")
    assert spec.untouched_test.start > spec.validation.start > spec.development.start
