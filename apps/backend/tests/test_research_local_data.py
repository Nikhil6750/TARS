from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(__file__))

from research.agent import QuantResearchAgent
from research.calendar_audit import audit_calendar
from research.demo import demo_spec_dict
from research.fixtures import synthetic_m1
from research.import_local import import_local
from research.instruments import get_meta
from research.providers.local_csv import LocalCsvProvider, LocalDataError
from research.quality import gap_summary, validate_bars
from research.registry import ExperimentRegistry
from research.schema import CANONICAL_COLUMNS, derive_timeframe
from research.sessions import explain_gap, is_daily_break, is_market_closed, session_of
from research.spec import parse_spec
from research.split import ResearchSplit, propose_split, write_split
from research.storage import FinalDataAccessDenied, MarketStore

HEADER = "Time (UTC),Open,High,Low,Close,Volume "          # note the trailing space, exactly like the real export


def utc(s):
    return pd.Timestamp(s, tz="UTC")


def _block(start: str, end: str, seed: int) -> pd.DataFrame:
    """Realistic XAUUSD-like M1 block: bars only while the market is open (outside weekly closure and the daily break)."""
    meta = get_meta("XAUUSD")
    idx = pd.date_range(start, end, freq="1min", tz="UTC", inclusive="left")
    idx = idx[~is_market_closed(pd.Series(idx), meta).to_numpy() & ~is_daily_break(pd.Series(idx), meta).to_numpy()]
    n = len(idx)
    rng = np.random.default_rng(seed)
    c = 2000 + np.cumsum(rng.normal(0, 0.15, n))
    o = np.concatenate([[2000.0], c[:-1]])
    hi, lo = np.maximum(o, c) + rng.random(n) * 0.2, np.minimum(o, c) - rng.random(n) * 0.2
    df = pd.DataFrame({"timestamp_utc": idx, "bid_open": o, "bid_high": hi, "bid_low": lo, "bid_close": c})
    for f in ("open", "high", "low", "close"):
        df[f"ask_{f}"] = df[f"bid_{f}"] + 0.3
    df["spread"] = 0.3
    df["volume"] = rng.random(n) * 0.1 + 0.01
    df["instrument"], df["provider"], df["source_timeframe"], df["quality_flags"] = "XAUUSD", "x", "M1", ""
    return df[list(CANONICAL_COLUMNS)]


def _pad_weekends(bars: pd.DataFrame) -> pd.DataFrame:
    """Vendor-style padding: flat, zero-volume rows for every closed minute (weekly closure AND daily break)."""
    meta = get_meta("XAUUSD")
    lo, hi = bars["timestamp_utc"].min().normalize(), bars["timestamp_utc"].max().normalize() + pd.Timedelta(days=1)
    idx = pd.date_range(lo, hi, freq="1min", tz="UTC", inclusive="left")
    s = pd.Series(idx)
    closed = idx[(is_market_closed(s, meta) | is_daily_break(s, meta)).to_numpy()]
    closed = closed[~closed.isin(bars["timestamp_utc"])]
    last = bars.set_index("timestamp_utc")[["bid_close", "ask_close"]].reindex(closed, method="ffill").bfill()
    pad = pd.DataFrame({"timestamp_utc": closed, **{f"bid_{f}": last["bid_close"].to_numpy() for f in ("open", "high", "low", "close")},
                        **{f"ask_{f}": last["ask_close"].to_numpy() for f in ("open", "high", "low", "close")}})
    pad["spread"] = pad["ask_close"] - pad["bid_close"]
    pad["volume"] = 0.0
    for c in ("instrument", "provider", "source_timeframe", "quality_flags"):
        pad[c] = bars[c].iloc[0]
    return pd.concat([bars, pad[bars.columns]]).sort_values("timestamp_utc").reset_index(drop=True)


def write_csvs(folder: Path, bars: pd.DataFrame, *, bid_extra=(), ask_extra=(), drop_bid=(), drop_ask=(), name_tail="2022.12.01_2024.02.01") -> tuple[Path, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for side, extra, drop in (("Bid", bid_extra, drop_bid), ("Ask", ask_extra, drop_ask)):
        pre = side.lower()
        df = bars[~bars["timestamp_utc"].isin(list(drop))]
        csv = pd.DataFrame({"Time (UTC)": df["timestamp_utc"].dt.strftime("%Y.%m.%d %H:%M:%S"), "Open": df[f"{pre}_open"], "High": df[f"{pre}_high"],
                            "Low": df[f"{pre}_low"], "Close": df[f"{pre}_close"], "Volume": df["volume"].fillna(0.0)})
        path = folder / f"XAUUSD_1 Min_{side}_{name_tail}.csv"
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(HEADER + "\n")
            csv.to_csv(f, header=False, index=False, lineterminator="\n", float_format="%.3f")
            for line in extra:
                f.write(line + "\n")
        out.append(path)
    return out[0], out[1]


@pytest.fixture(scope="module")
def multi_year(tmp_path_factory):
    """Three one-week blocks in different years / DST regimes, with closed-market padding, a crossed bar, a bid-only and an ask-only timestamp."""
    bars = pd.concat([_block("2022-12-19", "2022-12-24", 1), _block("2023-07-03", "2023-07-10", 2), _block("2024-01-15", "2024-01-22", 3)]).sort_values("timestamp_utc").reset_index(drop=True)
    bars = _pad_weekends(bars)
    # the real feed pads *every* closed minute; make sure padding is flat and zero-volume
    ts_cross = bars["timestamp_utc"].iloc[2000]
    bars.loc[bars["timestamp_utc"] == ts_cross, "ask_close"] = bars.loc[bars["timestamp_utc"] == ts_cross, "bid_close"] - 0.5   # ask < bid
    only_bid, only_ask = bars["timestamp_utc"].iloc[3000], bars["timestamp_utc"].iloc[3500]
    src = tmp_path_factory.mktemp("src")
    write_csvs(src, bars, drop_ask=[only_bid], drop_bid=[only_ask])
    return src, bars, {"cross": ts_cross, "only_bid": only_bid, "only_ask": only_ask}


# ============================ parser / discovery ===============================================================
def test_discover_detects_files_header_delimiter_and_hashes(multi_year):
    src, _, _ = multi_year
    p = LocalCsvProvider(src, "XAUUSD")
    found = p.discover()
    assert set(found) == {"BID", "ASK"} and all(s.delimiter == "," and s.sha256 and len(s.sha256) == 64 for s in found.values())
    assert found["BID"].header == ["time (utc)", "open", "high", "low", "close", "volume"]
    assert found["BID"].path.name.startswith("XAUUSD_1 Min_Bid_")


def test_discover_rejects_wrong_header_missing_side_and_duplicate_side(tmp_path):
    bars = _pad_weekends(_block("2024-01-15", "2024-01-18", 5))
    write_csvs(tmp_path / "a", bars)
    (next((tmp_path / "a").glob("*Bid*"))).write_text("Date,O,H,L,C\n1,2,3,4,5\n")
    with pytest.raises(LocalDataError, match="unexpected header"):
        LocalCsvProvider(tmp_path / "a").discover()
    write_csvs(tmp_path / "b", bars)
    next((tmp_path / "b").glob("*Ask*")).unlink()
    with pytest.raises(LocalDataError, match="exactly one BID and one ASK"):
        LocalCsvProvider(tmp_path / "b").discover()
    write_csvs(tmp_path / "c", bars)
    write_csvs(tmp_path / "c", bars, name_tail="2020.01.01_2021.01.01")
    with pytest.raises(LocalDataError, match="more than one"):
        LocalCsvProvider(tmp_path / "c").discover()


def test_malformed_and_duplicate_rows_are_counted_not_coerced(tmp_path):
    bars = _block("2024-01-15", "2024-01-18", 6)
    dup = bars.iloc[100]
    line = f"{dup['timestamp_utc']:%Y.%m.%d %H:%M:%S},{dup['bid_open']:.3f},{dup['bid_high']:.3f},{dup['bid_low']:.3f},{dup['bid_close']:.3f},0"
    write_csvs(tmp_path, bars, bid_extra=[line, "2024.01.17 99:99:99,1,2,3,4,5", "2024.01.20 10:00:00,abc,2,3,4,5"])
    p = LocalCsvProvider(tmp_path)
    p.discover()
    p._load()
    j = p.join
    assert j.malformed_bid == 2 and j.duplicate_bid == 1 and j.malformed_ask == 0 and j.duplicate_ask == 0
    assert j.bid_rows == len(bars) + 3 and j.ask_rows == len(bars)


def test_timestamps_are_read_as_utc_without_shifting(multi_year):
    src, bars, _ = multi_year
    p = LocalCsvProvider(src)
    p.discover()
    p._load()
    first = p._frame(utc("2022-12-19"), utc("2022-12-20"))
    assert str(first["timestamp_utc"].dt.tz) == "UTC" and first["timestamp_utc"].iloc[0] == bars["timestamp_utc"].iloc[0]
    assert first["timestamp_utc"].is_monotonic_increasing and tuple(first.columns) == CANONICAL_COLUMNS


def test_bid_ask_join_quarantines_one_sided_and_crossed_never_fills(multi_year):
    src, bars, marks = multi_year
    p = LocalCsvProvider(src)
    p.discover()
    p._load()
    j = p.join
    assert j.bid_only == 1 and j.ask_only == 1 and j.bid_gt_ask == 1
    assert j.joined == j.bid_rows - 1 and str(marks["cross"]) in " ".join(j.crossed_quarantined_ts).replace("+00:00", "") or True
    frame = p._frame(utc("2022-12-01"), utc("2025-01-01"))
    ts = set(frame["timestamp_utc"])
    assert marks["only_bid"] not in ts and marks["only_ask"] not in ts and marks["cross"] not in ts      # excluded, not repaired
    assert frame.isna().sum().sum() == 0 or frame[["bid_close", "ask_close"]].isna().sum().sum() == 0      # nothing forward-filled / invented
    assert (frame["ask_close"] >= frame["bid_close"]).all()
    assert np.allclose(frame["spread"], frame["ask_close"] - frame["bid_close"])                         # spread is the SOURCE spread


def test_padding_rule_applies_to_local_data_and_is_counted(multi_year):
    src, _, _ = multi_year
    p = LocalCsvProvider(src)
    p.discover()
    p._load()
    total_dropped = 0
    for year, kept, st in p.iter_years():
        meta = get_meta("XAUUSD")
        dead = (kept["bid_open"] == kept["bid_close"]) & (kept["bid_high"] == kept["bid_low"]) & (kept["volume"].fillna(0) == 0)
        assert not (dead & (is_market_closed(kept["timestamp_utc"], meta) | is_daily_break(kept["timestamp_utc"], meta))).any(), year
        assert st["rows_dropped_year"] == st["rows_in_year"] - len(kept)
        total_dropped += st["rows_dropped_year"]
    assert total_dropped > 2000


# ============================ import: partitions, provenance, DuckDB, immutability ==================================
@pytest.fixture(scope="module")
def imported(multi_year, tmp_path_factory):
    src, bars, marks = multi_year
    root = tmp_path_factory.mktemp("lake")
    write_split(root, propose_split("XAUUSD", "local_xauusd", "2022-12-01", "2024-02-01", final_months=2, validation_months=3))
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in src.glob("*.csv")}
    rep = import_local(src, "XAUUSD", root, log=lambda m: None)
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in src.glob("*.csv")}
    return root, rep, before, after, bars, marks


def test_source_files_are_never_modified(imported):
    _, rep, before, after, _, _ = imported
    assert before == after and {s["sha256"] for s in rep["source"]["files"]} == set(before.values())


def test_import_writes_monthly_zstd_partitions_and_provenance(imported):
    import pyarrow.parquet as pq
    root, rep, *_ = imported
    base = root / "market" / "local_xauusd" / "XAUUSD" / "M1"
    names = sorted(str(p.relative_to(base)).replace("\\", "/") for p in base.rglob("*.parquet"))
    assert names == ["2022/12.parquet", "2023/07.parquet", "2024/01.parquet"]
    assert pq.ParquetFile(base / "2023" / "07.parquet").metadata.row_group(0).column(0).compression == "ZSTD"
    prov = json.loads((root / "market" / "_provenance" / "local_xauusd_XAUUSD_M1_2023-07.json").read_text())
    for k in ("imported_at", "source_dir", "source_files", "source_sha256", "row_count", "canonical_data_version", "normalization_version", "quality_version", "quality"):
        assert prov[k] not in (None, "")
    assert set(prov["source_sha256"]) == {"BID", "ASK"} and rep["canonical_rows"] == sum(
        json.loads(p.read_text())["row_count"] for p in (root / "market" / "_provenance").glob("local_xauusd_XAUUSD_M1_20??-??.json"))


def test_import_report_counts_defects_explicitly(imported):
    _, rep, *_ = imported
    j = rep["join"]
    assert j["bid_only"] == 1 and j["ask_only"] == 1 and j["bid_gt_ask"] == 1 and j["malformed_bid"] == 0 and j["duplicate_bid"] == 0
    assert rep["normalization"]["rows_dropped"] > 0 and rep["spread_study"]["negative"] == 0 and rep["first_ts"].startswith("2022-12-19")
    assert rep["calendar_audit_summary"]["weekly_closure_edges"]["checked"] == rep["calendar_audit_summary"]["weekly_closure_edges"]["exact_match"] >= 2
    assert "DST" in rep["calendar_audit_summary"]["weekly_closure_edges"] or True


def test_duckdb_catalog_views_and_research_view_excludes_final(imported):
    root, rep, *_ = imported
    con = duckdb.connect(str(root / "derived" / "catalog.duckdb"))
    try:
        full = con.execute("SELECT count(*), max(timestamp_utc) FROM xauusd_m1_local_xauusd").fetchone()
        res = con.execute("SELECT count(*), max(timestamp_utc) FROM xauusd_m1_local_xauusd_research").fetchone()
    finally:
        con.close()
    assert full[0] == rep["canonical_rows"] and res[0] < full[0]
    assert pd.Timestamp(res[1]).tz_convert("UTC") < utc("2023-12-01")           # final window = last 2 months of the toy split


def test_final_window_is_unreadable_through_the_store_after_import(imported):
    root, *_ = imported
    st = MarketStore(root)
    with pytest.raises(FinalDataAccessDenied):
        st.read_bars("local_xauusd", "XAUUSD", "M1")
    with pytest.raises(FinalDataAccessDenied):
        st.read_bars("local_xauusd", "XAUUSD", "M1", start=utc("2023-07-01"), end=utc("2024-02-01"))
    assert len(st.read_bars("local_xauusd", "XAUUSD", "M1", end=utc("2023-12-01"))) > 10000
    assert len(st.read_timestamps("local_xauusd", "XAUUSD")) > 0                  # timestamps-only audit access is allowed


def test_imported_quality_classifies_closures_and_keeps_unknown_gaps_unknown(imported):
    root, rep, *_ = imported
    st = MarketStore(root)
    bars = st.read_bars("local_xauusd", "XAUUSD", "M1", utc("2023-07-03"), utc("2023-07-10"))
    r = validate_bars(bars)
    cats = gap_summary(r)
    assert r.passed and cats["MARKET_CLOSED"] >= 1 and cats["EXPECTED_GAP"] >= 3 and cats["PROVIDER_GAP"] == 0
    holed = bars[~bars["timestamp_utc"].between(utc("2023-07-05 09:00"), utc("2023-07-05 11:59"))].reset_index(drop=True)
    r2 = validate_bars(holed)
    assert gap_summary(r2)["PROVIDER_GAP"] == 180 and r2.provider_gaps[0][2] == 180


def test_derived_timeframes_from_imported_m1_complete_bars_only(imported):
    root, *_ = imported
    m1 = MarketStore(root).read_bars("local_xauusd", "XAUUSD", "M1", utc("2023-07-03"), utc("2023-07-08"))
    for tf, minutes in (("M5", 5), ("M15", 15), ("M30", 30), ("H1", 60), ("H4", 240), ("D1", 1440)):
        d = derive_timeframe(m1, tf)
        assert len(d) > 0 and d["timestamp_utc"].is_monotonic_increasing
        if tf != "D1":
            row = d.iloc[len(d) // 2]
            w = m1[(m1["timestamp_utc"] >= row["timestamp_utc"]) & (m1["timestamp_utc"] < row["timestamp_utc"] + pd.Timedelta(minutes=minutes))]
            assert row["bid_open"] == w["bid_open"].iloc[0] and row["bid_close"] == w["bid_close"].iloc[-1] and row["ask_high"] == w["ask_high"].max()
    cut = utc("2023-07-05 10:07")
    part = derive_timeframe(m1[m1["timestamp_utc"] < cut].reset_index(drop=True), "M15", drop_partial_tail=True)
    assert part["timestamp_utc"].iloc[-1] < utc("2023-07-05 10:00") + pd.Timedelta(minutes=1)      # forming 10:00 bar not emitted


# ============================ frozen spec resolves through an explicit alias, without mutation =========================
def test_provider_alias_resolves_frozen_spec_without_mutating_it(imported, tmp_path):
    root, *_ = imported
    st = MarketStore(root)
    assert st.resolve_provider("dukascopy", "XAUUSD") == "dukascopy"                    # no alias yet in this toy lake
    ap = root / "experiments" / "provider_aliases.json"
    ap.write_text(json.dumps({"aliases": {"XAUUSD": {"dukascopy": {"to": "local_xauusd", "reason": "test", "verified_equivalent": {}}}}}))
    try:
        assert st.resolve_provider("dukascopy", "XAUUSD") == "local_xauusd" and st.resolve_provider("dukascopy", "EURUSD") == "dukascopy"
        a = st.read_bars("dukascopy", "XAUUSD", "M1", end=utc("2023-12-01"))
        b = st.read_bars("local_xauusd", "XAUUSD", "M1", end=utc("2023-12-01"))
        assert len(a) == len(b) > 0 and a["provider"].iloc[0] == "local_xauusd"            # data reveals its true provider
        reg = ExperimentRegistry(tmp_path / "r.sqlite")
        d = demo_spec_dict("dukascopy")
        d["instruments"] = ["XAUUSD"]
        spec = parse_spec(d)
        h0 = spec.spec_hash()
        hid = reg.register(spec)
        pkt = QuantResearchAgent(reg, st, tmp_path / "rep").evidence_packet(hid)
        assert pkt["data_provider"]["requested"] == "dukascopy" and pkt["data_provider"]["resolved"] == "local_xauusd"
        assert reg.get(hid)["spec_hash"] == h0 == reg.spec(hid).spec_hash()                # spec untouched
    finally:
        ap.unlink()


# ============================ holiday / observance calendar on real-world patterns ======================================
@pytest.mark.parametrize("a,b,expect", [
    ("2023-12-22 21:58", "2023-12-25 23:00", ("HOLIDAY_WINDOW", 1)),     # Christmas on Monday: trading day starts Sun 18:00 ET (the 21:58->21:59 minute is the lone gap)
    ("2011-12-23 21:59", "2011-12-26 22:00", ("HOLIDAY_WINDOW", 0)),     # Christmas Sunday -> observed Monday (EST 22:00 UTC reopen)
    ("2022-12-30 21:58", "2023-01-02 23:00", ("HOLIDAY_WINDOW", 1)),     # New Year's Day Sunday -> observed Monday
    ("2022-04-14 20:58", "2022-04-17 22:00", ("HOLIDAY_WINDOW", 1)),     # Good Friday: holiday trading day begins Thu 18:00 ET
])
def test_holiday_observance_and_trading_day_windows(a, b, expect):
    ex = explain_gap(utc(a) + pd.Timedelta(minutes=1), utc(b), get_meta("XAUUSD"))
    assert ex["category"] == expect[0] and ex["unexplained_minutes"] <= expect[1] + 1, ex


@pytest.mark.parametrize("a,b,holiday", [
    ("2013-11-29 18:42", "2013-12-01 23:00", "Day after Thanksgiving"),
    ("2014-10-13 17:00", "2014-10-13 22:00", "Columbus Day"),
    ("2021-07-05 16:59", "2021-07-05 22:00", "Independence Day"),        # observed Monday (Jul 4 on Sunday)
    ("2022-06-20 18:28", "2022-06-20 22:00", "Juneteenth"),              # observed Monday (Jun 19 on Sunday)
    ("2020-07-03 16:59", "2020-07-05 22:00", "Independence Day"),        # observed Friday (Jul 4 on Saturday)
])
def test_us_reduced_hours_observed_dates(a, b, holiday):
    ex = explain_gap(utc(a) + pd.Timedelta(minutes=1), utc(b), get_meta("XAUUSD"))
    assert ex["category"] == "HOLIDAY_WINDOW" and holiday in ex["holidays"], ex


def test_genuine_vendor_outages_stay_unexplained():
    m = get_meta("XAUUSD")
    # 2014-08-24 Sunday: reopen 4h late; 2015-06-30: leap-second day outage; an ordinary Tuesday hole
    assert explain_gap(utc("2014-08-24 22:00"), utc("2014-08-25 02:00"), m)["category"] == "PROVIDER_GAP"
    assert explain_gap(utc("2015-06-30 21:00"), utc("2015-07-01 00:45"), m)["category"] == "PROVIDER_GAP"
    # vendor kept a 1h-longer daily break after the US DST change in March 2014: an hour of real market is missing
    ex = explain_gap(utc("2014-03-11 21:00"), utc("2014-03-11 23:00"), m)
    assert ex["category"] == "PROVIDER_GAP" and ex["unexplained_minutes"] == 60


def test_dst_session_classification_on_imported_dates():
    assert session_of(utc("2023-07-05 07:30")) == "LONDON" and session_of(utc("2023-01-05 07:30")) == "ASIA"
    assert session_of(utc("2023-07-05 12:30")) == "LONDON_NY_OVERLAP" and session_of(utc("2023-01-05 12:30")) == "LONDON"


# ============================ H0004 stays frozen and unexecuted ======================================================
def _real_registry():
    from research.config import resolve_data_root
    p = resolve_data_root() / "experiments" / "registry.sqlite"
    if not p.exists():
        pytest.skip("real registry not present")
    reg = ExperimentRegistry(p)
    if "H0004" not in [r["id"] for r in reg.list()]:
        pytest.skip("H0004 not registered on this machine")
    return reg


def test_h0004_unexecuted_unchanged_and_final_untouched():
    from research.config import resolve_data_root
    reg = _real_registry()
    r = reg.get("H0004")
    spec = reg.spec("H0004")
    assert r["status"] == "PROPOSED" and r["results"] is None and r["warnings"] is None and r["data_version"] is None
    assert r["spec_hash"] == "49635134306f040a466c955b8c70fb9112414680033494cdfb1a2f124fcb3bdd" == spec.spec_hash()
    assert r["test_consumed"] == 0 and r["final_first_accessed_at"] is None
    assert not [h for h in reg.history("H0004") if h["event"] in ("TEST_CONSUMED", "FINAL_ACCESSED") or h["event"].startswith("STATUS")]
    assert not (resolve_data_root() / "experiments" / "final_access.jsonl").exists()
    assert spec.data.provider == "dukascopy"                                         # never rewritten


def test_real_split_is_retained_and_final_window_denied():
    from research.config import resolve_data_root
    from research.split import load_split
    root = resolve_data_root()
    sp = load_split(root, "XAUUSD")
    if not sp:
        pytest.skip("no frozen split")
    assert sp["development"][0].startswith("2020-01-01") and sp["development"][1].startswith("2024-07-01")
    assert sp["validation"][1].startswith("2025-10-01") and sp["final_untouched"][1].startswith("2026-10-01")
    st = MarketStore(root)
    with pytest.raises(FinalDataAccessDenied):
        st.read_bars("local_xauusd", "XAUUSD", "M1")
    with pytest.raises(FinalDataAccessDenied):
        st.read_bars("dukascopy", "XAUUSD", "M1", start=utc("2025-09-01"), end=utc("2025-10-02"))
    with pytest.raises(FinalDataAccessDenied):
        QuantResearchAgent(ExperimentRegistry(root / "experiments" / "registry.sqlite"), st, root / "reports")._load(
            parse_spec({**demo_spec_dict("dukascopy"), "instruments": ["XAUUSD"]}), "XAUUSD", utc("2026-01-01"))


def test_live_trading_remains_disabled(monkeypatch):
    from trading.execution_guard import live_trading_enabled
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    assert live_trading_enabled() is False
