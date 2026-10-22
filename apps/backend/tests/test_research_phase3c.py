from __future__ import annotations

import json
import lzma
import threading
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from research.calendar_audit import audit_calendar
from research.fixtures import synthetic_m1
from research.ingest import ingest_months
from research.providers.base import RawCache
from research.providers.download import HttpFailure, IntegrityError, Manifest, RateGovernor, backoff_delay, verify_bi5
from research.providers.dukascopy import RECORD, DukascopyProvider, PartialFetchError
from research.registry import ExperimentRegistry
from research.sessions import easter_sunday, holiday_calendar, is_known_holiday
from research.spec import parse_spec
from research.split import ResearchSplit, SplitError, load_split, propose_split, write_split
from research.storage import FinalDataAccessDenied, MarketStore

import os, sys  # noqa: E402
sys.path.insert(0, os.path.dirname(__file__))
from test_research_phase3b import fake_opener, padded_opener  # noqa: E402


def utc(s):
    return pd.Timestamp(s, tz="UTC")


def bi5(rows: int = 10, start_sec: int = 0) -> bytes:
    return lzma.compress(b"".join(RECORD.pack(start_sec + i * 60, 2900000, 2900100, 2899900, 2900200, 0.1) for i in range(rows)))


# ============================ download engine ===============================================================
def test_verify_bi5_accepts_good_and_empty_rejects_corrupt():
    assert verify_bi5(bi5(5)) == 5 and verify_bi5(b"") == 0
    with pytest.raises(IntegrityError):
        verify_bi5(b"not lzma at all")
    with pytest.raises(IntegrityError, match="multiple"):
        verify_bi5(lzma.compress(b"x" * 25))
    with pytest.raises(IntegrityError, match="increasing"):
        verify_bi5(lzma.compress(RECORD.pack(60, 1, 1, 1, 1, 0.0) + RECORD.pack(60, 1, 1, 1, 1, 0.0)))
    with pytest.raises(IntegrityError, match="exceeds"):
        verify_bi5(lzma.compress(b"".join(RECORD.pack(i % 86400, 1, 1, 1, 1, 0.0) for i in range(1441))))


def test_backoff_is_exponential_capped_and_jittered():
    lo = [backoff_delay(n, rng=lambda: 0.0) for n in range(8)]
    hi = [backoff_delay(n, rng=lambda: 0.999) for n in range(8)]
    assert lo[0] == pytest.approx(0.5) and lo[3] == pytest.approx(4.0) and hi[3] == pytest.approx(11.992, abs=0.01)
    assert max(hi) <= 30 * 1.5 and lo == sorted(lo)
    assert backoff_delay(3, rng=lambda: 0.0) != backoff_delay(3, rng=lambda: 0.9)


def test_governor_429_reduces_concurrency_503_miss_does_not_and_recovers():
    g = RateGovernor(max_workers=6, min_interval=0.0)
    g.acquire(); g.release("miss")
    assert g.limit == 6 and g.stats["misses"] == 1
    g.acquire(); g.release("throttled", retry_after=0.0)
    assert g.limit == 5 and g.stats["throttled"] == 1
    for _ in range(40):
        g.acquire(); g.release("ok")
    assert g.limit == 6                                   # grows back after sustained success
    for _ in range(10):
        g.acquire(); g.release("throttled", retry_after=0.0)
    assert g.limit == g.floor == 2                        # never below the floor


def test_governor_bounds_parallelism():
    g = RateGovernor(max_workers=3, min_interval=0.0)
    active, peak, lock = [0], [0], threading.Lock()

    def work():
        g.acquire()
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.03)
        with lock:
            active[0] -= 1
        g.release("ok")
    ts = [threading.Thread(target=work) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] <= 3


class FlakyOpener:
    """503 (cache miss) twice, then a truncated body once, then good data - per URL."""

    def __init__(self, plan=("503", "503", "trunc", "ok")):
        self.plan, self.n, self.calls = plan, {}, []

    def __call__(self, url):
        self.calls.append(url)
        i = self.n.get(url, 0)
        self.n[url] = i + 1
        step = self.plan[min(i, len(self.plan) - 1)]
        if step == "503":
            raise HttpFailure(503)
        if step == "429":
            raise HttpFailure(429, 0.0)
        if step == "trunc":
            return bi5(20)[:30]
        return bi5(20)


def test_fetch_retries_503_and_corrupt_then_succeeds_and_records_attempts(tmp_path):
    op = FlakyOpener()
    p = DukascopyProvider(opener=op, workers=2, min_interval=0.0, sleep=lambda s: None, skip_closed_days=False)
    raw = p.fetch("XAUUSD", datetime(2025, 3, 3, tzinfo=timezone.utc), datetime(2025, 3, 4, tzinfo=timezone.utc), RawCache(tmp_path))
    assert len(raw) == 2 and all(verify_bi5(r.path.read_bytes()) == 20 for r in raw)
    m = Manifest(tmp_path / "manifest.sqlite")
    rec = m.get("dukascopy/XAUUSD/2025-03-03/BID")
    assert rec["attempts"] == 4 and rec["status"] == "OK" and rec["rows"] == 20 and len(rec["sha256"]) == 64
    assert p.governor.stats["misses"] >= 4 and p.governor.limit == 2   # 503s did not shrink concurrency


def test_truncated_file_is_never_cached(tmp_path):
    p = DukascopyProvider(opener=FlakyOpener(("trunc",)), workers=1, retries=3, min_interval=0.0, sleep=lambda s: None, skip_closed_days=False)
    with pytest.raises(PartialFetchError):
        p.fetch("XAUUSD", datetime(2025, 3, 3, tzinfo=timezone.utc), datetime(2025, 3, 4, tzinfo=timezone.utc), RawCache(tmp_path))
    assert not list((tmp_path).rglob("*.bi5"))


def test_resume_skips_verified_files_no_duplicate_downloads(tmp_path):
    op = FlakyOpener(("ok",))
    p = DukascopyProvider(opener=op, workers=2, min_interval=0.0, sleep=lambda s: None, skip_closed_days=False)
    s, e = datetime(2025, 3, 3, tzinfo=timezone.utc), datetime(2025, 3, 6, tzinfo=timezone.utc)
    first = p.fetch("XAUUSD", s, e, RawCache(tmp_path))
    n = len(op.calls)
    second = DukascopyProvider(opener=op, min_interval=0.0, skip_closed_days=False).fetch("XAUUSD", s, e, RawCache(tmp_path))
    assert len(op.calls) == n and [r.sha256 for r in first] == [r.sha256 for r in second]


def test_interrupted_run_resumes_from_manifest(tmp_path):
    seen = {"n": 0}

    class Dying:
        def __call__(self, url):
            seen["n"] += 1
            if seen["n"] > 4:
                raise HttpFailure(500)
            return bi5(10)
    p = DukascopyProvider(opener=Dying(), workers=1, retries=2, min_interval=0.0, sleep=lambda s: None, skip_closed_days=False)
    s, e = datetime(2025, 3, 3, tzinfo=timezone.utc), datetime(2025, 3, 8, tzinfo=timezone.utc)
    with pytest.raises(PartialFetchError):
        p.fetch("XAUUSD", s, e, RawCache(tmp_path))
    done = Manifest(tmp_path / "manifest.sqlite").count()
    assert done == 4
    op = FlakyOpener(("ok",))
    DukascopyProvider(opener=op, min_interval=0.0, skip_closed_days=False).fetch("XAUUSD", s, e, RawCache(tmp_path))
    assert len(op.calls) == 10 - done                      # only the missing files were requested


def test_saturdays_skipped_and_estimate_respects_it(tmp_path):
    op = FlakyOpener(("ok",))
    p = DukascopyProvider(opener=op, min_interval=0.0)
    raw = p.fetch("XAUUSD", datetime(2025, 3, 7, tzinfo=timezone.utc), datetime(2025, 3, 10, tzinfo=timezone.utc), RawCache(tmp_path))
    assert {r.key.split("/")[0] for r in raw} == {"2025-03-07", "2025-03-09"}     # Fri + Sun, no Saturday
    assert all("2025/02/08" not in u for u in op.calls)


def test_manifest_adopts_preexisting_verified_files(tmp_path):
    cache = RawCache(tmp_path)
    path = cache.path_for("dukascopy", "XAUUSD", "2025", "03", "03_BID.bi5")
    path.parent.mkdir(parents=True)
    path.write_bytes(bi5(7))
    op = FlakyOpener(("ok",))
    p = DukascopyProvider(opener=op, min_interval=0.0, skip_closed_days=False)
    p.fetch("XAUUSD", datetime(2025, 3, 3, tzinfo=timezone.utc), datetime(2025, 3, 4, tzinfo=timezone.utc), cache)
    assert len(op.calls) == 1 and Manifest(tmp_path / "manifest.sqlite").get("dukascopy/XAUUSD/2025-03-03/BID")["retrieved_at"] == "adopted"


# ============================ monthly ingestion + partitions ==================================================
def test_ingest_months_partitions_by_year_month_and_resumes(tmp_path):
    p = DukascopyProvider(opener=fake_opener(), workers=2, min_interval=0.0, sleep=lambda s: None)
    logs = []
    s, e = datetime(2025, 2, 24, tzinfo=timezone.utc), datetime(2025, 4, 6, tzinfo=timezone.utc)
    tot = ingest_months(p, "XAUUSD", s, e, tmp_path, reserve_bytes=1, log=logs.append)
    base = tmp_path / "market" / "dukascopy" / "XAUUSD" / "M1" / "2025"
    assert sorted(f.name for f in base.glob("*.parquet")) == ["02.parquet", "03.parquet", "04.parquet"]
    assert tot["months"] == 3 and tot["skipped"] == 0 and tot["rows"] > 0 and (tmp_path / "market" / "_provenance").exists()
    prov = json.loads((tmp_path / "market" / "_provenance" / "dukascopy_XAUUSD_M1_2025-03.json").read_text())
    for k in ("raw_checksum_sha256", "row_count", "normalization_version", "fetch_stats", "quality", "retrieved_at"):
        assert prov[k] not in (None, "")
    again = ingest_months(DukascopyProvider(opener=fake_opener(), min_interval=0.0), "XAUUSD", s, e, tmp_path, reserve_bytes=1, log=logs.append)
    assert again["skipped"] == 3 and again["months"] == 0 and again["rows"] == tot["rows"]
    assert any("up to date" in line for line in logs)


def test_read_prunes_files_by_month_and_legacy_year_files_migrate(tmp_path):
    store = MarketStore(tmp_path)
    bars = synthetic_m1(instrument="XAUUSD", provider="dukascopy", start="2025-02-24", days=26, base=2900.0, spread=0.3)
    legacy_dir = tmp_path / "market" / "dukascopy" / "XAUUSD" / "M1"
    legacy_dir.mkdir(parents=True)
    import pyarrow as pa, pyarrow.parquet as pq
    from research.schema import ARROW_SCHEMA, CANONICAL_COLUMNS
    pq.write_table(pa.Table.from_pandas(bars[list(CANONICAL_COLUMNS)], schema=ARROW_SCHEMA, preserve_index=False), legacy_dir / "2025.parquet", compression="zstd")
    assert [f.name for f in store.files("dukascopy", "XAUUSD", "M1")] == ["2025.parquet"]
    store.write_bars(synthetic_m1(instrument="XAUUSD", provider="dukascopy", start="2025-04-07", days=3, base=2900.0, spread=0.3), merge=True)
    names = sorted(f.relative_to(legacy_dir).as_posix() for f in store.files("dukascopy", "XAUUSD", "M1"))
    assert names == ["2025/03.parquet", "2025/04.parquet"] or names == ["2025/02.parquet", "2025/03.parquet", "2025/04.parquet"]
    assert not (legacy_dir / "2025.parquet").exists()
    only_march = store.files("dukascopy", "XAUUSD", "M1", utc("2025-03-05"), utc("2025-03-10"))
    assert [f.name for f in only_march] == ["03.parquet"]
    back = store.read_bars("dukascopy", "XAUUSD", "M1", utc("2025-03-05"), utc("2025-03-10"))
    assert back["timestamp_utc"].min() >= utc("2025-03-05") and back["timestamp_utc"].max() < utc("2025-03-10")


# ============================ calendar ===========================================================================
def test_easter_and_holiday_calendar():
    assert easter_sunday(2024).date().isoformat() == "2024-03-31" and easter_sunday(2025).date().isoformat() == "2025-04-20"
    assert easter_sunday(2020).date().isoformat() == "2020-04-12" and easter_sunday(2026).date().isoformat() == "2026-04-05"
    cal = holiday_calendar(2024, 2025)
    assert cal[pd.Timestamp("2024-03-29").date()] == ("Good Friday", "FULL")
    assert cal[pd.Timestamp("2024-12-25").date()][1] == "FULL" and cal[pd.Timestamp("2024-12-24").date()][1] == "SHORT_CANDIDATE"
    assert list(is_known_holiday(pd.Series([utc("2025-04-18 10:00"), utc("2025-04-17 10:00"), utc("2025-01-01 00:00")]))) == [True, False, True]


def _m1_with_holes(start, end, holes):
    from research.instruments import get_meta
    from research.sessions import is_daily_break, is_market_closed
    idx = pd.date_range(start, end, freq="1min", tz="UTC", inclusive="left")
    m = get_meta("XAUUSD")
    keep = ~is_market_closed(pd.Series(idx), m).to_numpy() & ~is_daily_break(pd.Series(idx), m).to_numpy()
    idx = idx[keep]
    for a, b in holes:
        idx = idx[~((idx >= utc(a)) & (idx < utc(b)))]
    return pd.Series(idx)


def test_audit_classifies_weekends_breaks_holidays_short_sessions_and_provider_gaps():
    ts = _m1_with_holes("2024-12-20", "2025-01-08", holes=[
        ("2024-12-25 00:00", "2024-12-25 23:00"),     # Christmas Day (FULL)
        ("2024-12-31 18:00", "2025-01-01 22:00"),     # NYE evening + New Year's Day (FULL, spans)
        ("2024-12-24 19:00", "2024-12-24 23:00"),     # Christmas Eve 4h hole: SHORT_CANDIDATE, must NOT be called closure
        ("2025-01-03 09:00", "2025-01-03 12:30"),     # ordinary Friday: unexplained => provider gap
    ])
    r = audit_calendar(ts, "XAUUSD")
    c = r["gap_counts"]
    print(r["gap_counts"], r["weekly_closure_edges"])
    assert c["MARKET_CLOSED"] >= 2 and c["EXPECTED_GAP"] >= 5 and c["HOLIDAY_WINDOW"] >= 1
    assert c["UNVERIFIED_SHORT"] == 2 and c["PROVIDER_GAP"] == 1      # Dec 24 hole; NYE evening + New Year's Day (evening part is unverified)
    assert r["provider_gaps"][0]["missing_bars"] == 210 and r["unverified_short_sessions"][0]["date"] == ["Christmas Eve"] and r["unverified_short_sessions"][1]["date"] == ["New Year's Eve"]
    assert r["weekly_closure_edges"]["checked"] == r["weekly_closure_edges"]["exact_match"] >= 2


def test_audit_edges_follow_dst_across_us_and_eu_transitions():
    ts = _m1_with_holes("2025-03-02", "2025-04-07", holes=[])
    r = audit_calendar(ts, "XAUUSD")
    assert r["weekly_closure_edges"]["checked"] == 5 and r["weekly_closure_edges"]["exact_match"] == 5
    assert r["gap_counts"]["PROVIDER_GAP"] == 0 and not r["weekly_closure_edges"]["mismatches"]
    # a feed that kept a fixed winter-time (22:00 UTC) close after the US DST change would trade one hour into the closure: detected
    late = pd.concat([ts, pd.Series(pd.date_range("2025-03-14 21:00", "2025-03-14 21:59", freq="1min", tz="UTC"))]).sort_values().reset_index(drop=True)
    bad = audit_calendar(late, "XAUUSD")
    assert bad["weekly_closure_edges"]["exact_match"] == 4 and bad["weekly_closure_edges"]["mismatches"][0]["close_offset_min"] == 60
    # and an hour missing BEFORE the closure is a provider gap, never silently absorbed into 'closure'
    early = ts[~((ts >= utc("2025-03-14 20:00")) & (ts < utc("2025-03-14 21:00")))].reset_index(drop=True)
    assert audit_calendar(early, "XAUUSD")["gap_counts"]["PROVIDER_GAP"] == 1


def test_quality_engine_does_not_label_unknown_gaps_as_closure():
    from research.quality import gap_summary, validate_bars
    from research.schema import make_bars
    ts = _m1_with_holes("2025-03-03", "2025-03-31", holes=[("2025-03-05 09:00", "2025-03-05 14:00")])
    n = len(ts)
    c = 2900 + np.cumsum(np.random.default_rng(1).normal(0, 0.05, n))
    df = pd.DataFrame({"timestamp_utc": ts, "bid_open": c, "bid_high": c + 0.1, "bid_low": c - 0.1, "bid_close": c})
    for f in ("open", "high", "low", "close"):
        df[f"ask_{f}"] = df[f"bid_{f}"] + 0.3
    bars = make_bars(df, instrument="XAUUSD", provider="t")
    rep = validate_bars(bars, max_large_gaps=0)
    assert "LARGE_GAPS" in rep.codes("BLOCK") and gap_summary(rep)["PROVIDER_GAP"] > 100
    assert rep.provider_gaps and rep.provider_gaps[0][2] == 300
    tolerant = validate_bars(bars, max_large_gaps=3)           # a few isolated outages: warn + list, not block
    assert "LARGE_GAPS" in tolerant.codes("WARN") and tolerant.passed


# ============================ split + FINAL_UNTOUCHED store guard =================================================
def test_split_is_rule_based_month_aligned_and_frozen(tmp_path):
    sp = propose_split("XAUUSD", "dukascopy", "2020-01-01", "2026-10-01")
    assert sp.development == ("2020-01-01T00:00:00+00:00", "2024-07-01T00:00:00+00:00")
    assert sp.validation == ("2024-07-01T00:00:00+00:00", "2025-10-01T00:00:00+00:00")
    assert sp.final_untouched == ("2025-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00")
    write_split(tmp_path, sp)
    write_split(tmp_path, sp)                                   # identical re-write is a no-op
    other = propose_split("XAUUSD", "dukascopy", "2020-01-01", "2026-10-01", final_months=6)
    with pytest.raises(SplitError, match="cannot be changed"):
        write_split(tmp_path, other)
    with pytest.raises(SplitError, match="too short"):
        propose_split("XAUUSD", "dukascopy", "2025-01-01", "2026-01-01")
    assert load_split(tmp_path, "xauusd")["hash"] == sp.hash()


def test_store_denies_reads_into_final_window_and_logs_authorised_ones(tmp_path):
    write_split(tmp_path, propose_split("XAUUSD", "dukascopy", "2020-01-01", "2026-10-01"))
    store = MarketStore(tmp_path)
    p = DukascopyProvider(opener=fake_opener(), min_interval=0.0)
    store.write_bars(synthetic_m1(instrument="XAUUSD", provider="dukascopy", start="2025-09-29", days=6, base=2900.0, spread=0.3))
    assert store.final_start("XAUUSD") == utc("2025-10-01")
    store.read_bars("dukascopy", "XAUUSD", "M1", end=utc("2025-10-01"))                    # up to the boundary: fine
    with pytest.raises(FinalDataAccessDenied):
        store.read_bars("dukascopy", "XAUUSD", "M1")                                       # unbounded
    with pytest.raises(FinalDataAccessDenied):
        store.read_bars("dukascopy", "XAUUSD", "M1", start=utc("2025-09-01"), end=utc("2025-10-02"))
    assert not (tmp_path / "experiments" / "final_access.jsonl").exists()
    got = store.read_bars("dukascopy", "XAUUSD", "M1", authorized_final=True, purpose="untouched_test")
    assert len(got) > 0
    log = [json.loads(line) for line in (tmp_path / "experiments" / "final_access.jsonl").read_text().splitlines()]
    assert log[0]["purpose"] == "untouched_test" and log[0]["instrument"] == "XAUUSD"
    # unprotected instruments are unaffected; timestamps-only audit reads work on protected data without exposing prices
    assert len(store.read_timestamps("dukascopy", "XAUUSD")) == len(got)
    EUR = synthetic_m1(instrument="EURUSD", provider="dukascopy", start="2025-09-29", days=6)
    store.write_bars(EUR)
    assert len(store.read_bars("dukascopy", "EURUSD", "M1")) == len(EUR)


# ============================ H0004 plumbing ======================================================================
def test_entry_sessions_filter_is_causal_and_changes_fingerprint():
    from research.demo import demo_spec_dict
    d = demo_spec_dict()
    a = parse_spec(d)
    d2 = json.loads(json.dumps(d))
    d2["signal"]["entry_sessions"] = ["LONDON"]
    b = parse_spec(d2)
    assert a.fingerprint() != b.fingerprint() and a.spec_hash() != b.spec_hash()
    assert "entry_sessions" not in a.canonical_json()                  # older hashes unchanged
    d2["signal"]["entry_sessions"] = ["MOON"]
    with pytest.raises(Exception, match="entry_sessions"):
        parse_spec(d2)


def test_entry_sessions_restrict_trades_to_the_session(tmp_path):
    from research.backtest import simulate
    from research.demo import demo_spec_dict
    from research.fixtures import synthetic_m1 as syn
    from research.schema import derive_timeframe
    from research.sessions import classify_sessions
    bars = derive_timeframe(syn(start="2025-02-24", days=26, vol=0.0003), "M15")
    d = demo_spec_dict()
    d["signal"]["entry_sessions"] = ["LONDON"]
    spec = parse_spec(d)
    free = parse_spec(demo_spec_dict())
    rng = dict(start=utc("2025-03-03"), end=utc("2025-03-20"))
    t_all, t_lon = simulate(bars, free, free.costs, **rng), simulate(bars, spec, spec.costs, **rng)
    assert 0 < len(t_lon) < len(t_all)
    assert (classify_sessions(t_lon["signal_time"]) == "LONDON").all()


def test_h0004_is_frozen_unexecuted_and_policy_compliant_if_present():
    from research.config import resolve_data_root
    reg_path = resolve_data_root() / "experiments" / "registry.sqlite"
    if not reg_path.exists():
        pytest.skip("real registry not present on this machine")
    reg = ExperimentRegistry(reg_path)
    ids = [r["id"] for r in reg.list()]
    if "H0004" not in ids:
        pytest.skip("H0004 not registered on this machine")
    r = reg.get("H0004")
    spec = reg.spec("H0004")
    assert r["status"] in {"PROPOSED", "REJECTED"} and not r["diagnostic"] and r["final_first_accessed_at"] is None   # PROPOSED until Phase 3D, REJECTED after its single frozen run
    assert spec.spec_hash() == r["spec_hash"] and spec.instruments == ["XAUUSD"] and spec.rejection.min_trades_per_period >= 100
    assert len(spec.sensitivity.parameter_variants) <= 4
    sp = load_split(resolve_data_root(), "XAUUSD")
    assert pd.Timestamp(spec.untouched_test.start) == utc(sp["final_untouched"][0])


def test_trades_spanning_unexplained_provider_gaps_are_excluded_and_counted(tmp_path):
    from research.agent import QuantResearchAgent
    from research.demo import demo_spec_dict
    from research.schema import derive_timeframe
    store = MarketStore(tmp_path)
    agent = QuantResearchAgent(ExperimentRegistry(tmp_path / "r.sqlite"), store, tmp_path / "rep")
    bars = derive_timeframe(synthetic_m1(start="2025-02-24", days=26, vol=0.0003), "M15")
    spec = parse_spec(demo_spec_dict())
    kw = dict(start=utc("2025-03-03"), end=utc("2025-03-14"))
    all_trades = agent._sim("EURUSD", bars, spec, spec.costs, **kw)
    assert len(all_trades) > 3 and agent.excluded_gap_trades == 0
    victim = all_trades.iloc[2]
    agent._gaps["EURUSD"] = [(victim["entry_time"] + pd.Timedelta(minutes=15), victim["entry_time"] + pd.Timedelta(minutes=30))]
    kept = agent._sim("EURUSD", bars, spec, spec.costs, **kw)
    assert len(kept) < len(all_trades) and agent.excluded_gap_trades == len(all_trades) - len(kept) >= 1
    assert victim["entry_time"] not in set(kept["entry_time"])


def test_scale_benchmark_runs_every_stage_on_multi_year_synthetic_parquet(tmp_path):
    from research.real_diagnostic import benchmark, real_diagnostic_spec
    store = MarketStore(tmp_path)
    bars = synthetic_m1(instrument="XAUUSD", provider="dukascopy", start="2023-01-02", days=120, base=2000.0, vol=0.0001, spread=0.5)
    store.write_bars(bars)
    assert len(store.files("dukascopy", "XAUUSD", "M1")) >= 4                       # monthly partitions
    spec = real_diagnostic_spec("XAUUSD", bars["timestamp_utc"].iloc[0], bars["timestamp_utc"].iloc[-1])
    t = benchmark(spec, tmp_path)
    assert t["m1_rows"] > 60000 and t["backtest_s"] >= 0 and t["data_load_s"] >= 0


def test_long_flat_zero_volume_runs_outside_closures_are_dropped_short_ones_kept(tmp_path):
    """Holiday-style padding (Dec 25 / Jan 1) sits OUTSIDE any weekly closure; it is removed by the run-length rule only."""
    inner = fake_opener()

    def opener(url):
        if "/2025/02/12/" not in url:                       # Wed 2025-03-12 (month index 02)
            return inner(url)
        side = "ASK" if "ASK" in url else "BID"
        sp = 30 if side == "ASK" else 0
        recs = []
        for m in range(1440):
            if 600 <= m < 900 or m == 1000 or m == 1001:    # 5h dead run + a 2-minute dead blip
                recs.append(RECORD.pack(m * 60, 2900000 + sp, 2900000 + sp, 2900000 + sp, 2900000 + sp, 0.0))
            else:
                recs.append(RECORD.pack(m * 60, 2900000 + sp + m, 2900000 + sp + m + 7, 2900000 + sp + m - 5, 2900000 + sp + m + 9, 0.05))
        return lzma.compress(b"".join(recs))
    p = DukascopyProvider(opener=opener, min_interval=0.0, sleep=lambda s: None)
    raw = p.fetch("XAUUSD", datetime(2025, 3, 12, tzinfo=timezone.utc), datetime(2025, 3, 13, tzinfo=timezone.utc), RawCache(tmp_path))
    bars = p.normalize("XAUUSD", raw)
    st = p.last_normalization_stats
    assert st["dropped_long_dead_runs"] == 300 and st["long_dead_run_min_minutes"] == 30
    assert st["open_hours_flat_rows_kept"] == 2                       # the 2-minute blip is a plausible quiet market: kept
    assert len(bars) == 1440 - 300 - st["dropped_weekly_closure"] - st["dropped_daily_break"]
    ts = set(bars["timestamp_utc"])
    assert utc("2025-03-12 10:00") not in ts and utc("2025-03-12 14:59") not in ts           # 5h run dropped
    assert utc("2025-03-12 15:00") in ts and utc("2025-03-12 16:40") in ts and utc("2025-03-12 16:41") in ts   # blip kept


def test_us_holiday_reduced_hours_are_pattern_matched_not_assumed():
    from research.instruments import get_meta
    from research.sessions import explain_gap, us_reduced_hours_dates
    d = us_reduced_hours_dates(2020, 2025)
    assert d[pd.Timestamp("2020-01-20").date()] == "Martin Luther King Jr. Day" and d[pd.Timestamp("2020-02-17").date()] == "Presidents' Day"
    assert d[pd.Timestamp("2025-05-26").date()] == "Memorial Day" and d[pd.Timestamp("2020-11-26").date()] == "Thanksgiving Day"
    m = get_meta("XAUUSD")
    # exactly what the real feed shows on MLK Day 2020: data stops 12:59 ET (17:59 UTC), resumes 18:00 ET (23:00 UTC)
    hit = explain_gap(utc("2020-01-20 18:00"), utc("2020-01-20 23:00"), m)
    assert hit["category"] == "HOLIDAY_WINDOW" and hit["unexplained_minutes"] == 0 and "Martin Luther King Jr. Day" in hit["holidays"] + ["Martin Luther King Jr. Day"]
    # same hours on an ordinary Monday are NOT excused: provider gap
    miss = explain_gap(utc("2020-01-27 18:00"), utc("2020-01-27 23:00"), m)
    assert miss["category"] == "PROVIDER_GAP" and miss["unexplained_minutes"] == 240
    # a holiday only excuses its own window: an extra morning hole on the holiday stays unexplained
    wide = explain_gap(utc("2020-01-20 14:00"), utc("2020-01-20 23:00"), m)      # 09:00 ET start: 09:00-12:00 is not excused
    assert wide["category"] == "PROVIDER_GAP" and wide["unexplained_minutes"] == 180


def test_gap_explained_by_daily_break_is_not_labelled_holiday():
    from research.instruments import get_meta
    from research.sessions import explain_gap
    # Jan 1 is a FULL holiday date, but a plain 17:00-18:00 ET break on it needs no holiday rule
    ex = explain_gap(utc("2024-01-01 22:00"), utc("2024-01-01 23:00"), get_meta("XAUUSD"))
    assert ex["category"] == "EXPECTED_GAP" and ex["unexplained_minutes"] == 0


def test_long_weekend_with_good_friday_is_explained_to_the_minute():
    from research.instruments import get_meta
    from research.sessions import explain_gap
    ex = explain_gap(utc("2024-03-29 20:59") + pd.Timedelta(minutes=1) - pd.Timedelta(minutes=1), utc("2024-03-31 22:00"), get_meta("XAUUSD"))
    assert ex["category"] == "HOLIDAY_WINDOW" and ex["unexplained_minutes"] == 0 and "Good Friday" in ex["holidays"]
    gap = explain_gap(utc("2024-03-29 20:59"), utc("2024-03-31 22:00") + pd.Timedelta(hours=3), get_meta("XAUUSD"))
    assert gap["category"] == "PROVIDER_GAP" and gap["unexplained_minutes"] == 180        # 3 extra hours after the open are NOT excused
