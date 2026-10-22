"""Validated import of a LOCAL historical BID+ASK CSV folder into the canonical research store.

    python -m research.import_local "C:\\Trade Data\\xauusd_utc" XAUUSD

source CSVs (immutable) -> discover/hash -> DuckDB raw-text staging -> bid/ask join (one-sided rows quarantined+counted)
-> padding rule (counted) -> canonical M1 -> per-month DataQualityReport -> monthly Parquet/ZSTD -> DuckDB catalog -> provenance
-> calendar audit (timestamps only) -> spread study (pre-FINAL only) -> equivalence cross-check -> provider alias for frozen specs."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .calendar_audit import audit_calendar
from .config import resolve_data_root
from .ingest import _dir_size, register_duckdb
from .instruments import get_meta
from .providers.local_csv import NORMALIZATION_VERSION, LocalCsvProvider
from .quality import QUALITY_VERSION, gap_summary, validate_bars
from .schema import CANONICAL_COLUMNS, PRICE_COLUMNS
from .sessions import full_holiday_trading_days, is_daily_break, is_market_closed
from .split import load_split
from .storage import MarketStore

PRICE_STAT_CODES = {"ABNORMAL_SPREAD", "PRICE_DISCONTINUITY"}   # price statistics are not recorded for the protected final window


def expected_trading_minutes(year: int, meta) -> int:
    """Minutes in the year inside neither the weekly closure, the daily break, nor a FULL holiday trading day."""
    mins = pd.Series(pd.date_range(f"{year}-01-01", f"{year + 1}-01-01", freq="1min", tz="UTC", inclusive="left"))
    local = mins.dt.tz_convert(meta.timezone)
    td = ((local.dt.tz_localize(None) - pd.Timedelta(hours=18)).dt.floor("1D") + pd.Timedelta(days=1)).dt.date
    full = td.isin(set(full_holiday_trading_days(year, year))).to_numpy()
    ok = ~(is_market_closed(mins, meta).to_numpy() | is_daily_break(mins, meta).to_numpy() | full)
    return int(ok.sum())


def spread_study(store: MarketStore, provider: str, instrument: str, end) -> dict:
    files = store.files(provider, instrument, "M1", end=end)
    q = store.query("""SELECT count(*) n, median(ask_close-bid_close) med, quantile_cont(ask_close-bid_close,0.9) p90,
                              quantile_cont(ask_close-bid_close,0.95) p95, quantile_cont(ask_close-bid_close,0.99) p99, max(ask_close-bid_close) mx,
                              min(ask_close-bid_close) mn, count(*) FILTER (WHERE ask_close-bid_close < 0) neg,
                              count(*) FILTER (WHERE ask_close-bid_close = 0) zero
                       FROM read_parquet(?) WHERE timestamp_utc < ?""",
                    [[str(f) for f in files], pd.Timestamp(end).tz_convert("UTC").to_pydatetime()]).iloc[0]
    med = float(q["med"])
    abn = int(store.query("SELECT count(*) c FROM read_parquet(?) WHERE timestamp_utc < ? AND ask_close-bid_close > ?",
                          [[str(f) for f in files], pd.Timestamp(end).tz_convert("UTC").to_pydatetime(), 8 * med]).iloc[0]["c"])
    return {"scope": f"all data before {end} (FINAL window excluded)", "rows": int(q["n"]), "median": med, "p90": float(q["p90"]),
            "p95": float(q["p95"]), "p99": float(q["p99"]), "max": float(q["mx"]), "min": float(q["mn"]),
            "negative": int(q["neg"]), "zero": int(q["zero"]), "abnormal_gt_8x_median": abn}


def cross_check_online(root: Path, store: MarketStore, instrument: str, local_provider: str) -> dict:
    """Compare the local canonical data to months already ingested from the online Dukascopy feed (complete months only)."""
    pdir = root / "market" / "_provenance"
    months = []
    for f in sorted(pdir.glob(f"dukascopy_{instrument}_M1_20??-??.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        rs, re_ = pd.Timestamp(d["requested_range"][0]), pd.Timestamp(d["requested_range"][1])
        if rs.day == 1 and (re_ - rs).days >= 28:
            months.append((d["month"], rs, re_))
    out = {"months_compared": 0, "rows_both": 0, "price_mismatch_rows": 0, "only_local": 0, "only_online": 0, "months": []}
    for label, rs, re_ in months:
        online_files = [p for p in (root / "market" / "dukascopy" / instrument / "M1").rglob(f"{label[5:]}.parquet") if p.parent.name == label[:4]]
        if not online_files:
            continue
        on = pd.read_parquet(online_files[0], columns=["timestamp_utc", *PRICE_COLUMNS])
        lo = store.read_bars(local_provider, instrument, "M1", rs, re_, purpose="cross_check")[["timestamp_utc", *PRICE_COLUMNS]]
        on["timestamp_utc"] = pd.to_datetime(on["timestamp_utc"], utc=True)
        m = on.merge(lo, on="timestamp_utc", how="outer", suffixes=("_on", "_lo"), indicator=True)
        both = m[m["_merge"] == "both"]
        mism = int((np.abs(both[[c + "_on" for c in PRICE_COLUMNS]].to_numpy() - both[[c + "_lo" for c in PRICE_COLUMNS]].to_numpy()) > 1e-9).any(axis=1).sum())
        row = {"month": label, "both": int(len(both)), "mismatch": mism, "only_local": int((m["_merge"] == "right_only").sum()),
               "only_online": int((m["_merge"] == "left_only").sum())}
        out["months"].append(row)
        out["months_compared"] += 1
        out["rows_both"] += row["both"]
        out["price_mismatch_rows"] += mism
        out["only_local"] += row["only_local"]
        out["only_online"] += row["only_online"]
    return out


def import_local(source_dir: str | Path, instrument: str, root: Path | None = None, provider_name: str = "local_xauusd", log=print) -> dict:
    root = Path(root) if root else resolve_data_root()
    t_all = time.perf_counter()
    timings: dict[str, float] = {}
    store = MarketStore(root)
    meta = get_meta(instrument)
    prov = LocalCsvProvider(source_dir, instrument, name=provider_name)
    tic = time.perf_counter()
    sources = prov.discover()
    timings["discover_and_hash_s"] = round(time.perf_counter() - tic, 2)
    tic = time.perf_counter()
    prov._load()
    timings["parse_stage_join_s"] = round(time.perf_counter() - tic, 2)
    j = prov.join
    log(f"parsed bid={j.bid_rows} ask={j.ask_rows} malformed={j.malformed_bid}/{j.malformed_ask} dup={j.duplicate_bid}/{j.duplicate_ask} "
        f"bid_only={j.bid_only} ask_only={j.ask_only} bid>ask={j.bid_gt_ask}")
    split = load_split(root, instrument)
    final_start = pd.Timestamp(split["final_untouched"][0]) if split else None

    pdir = root / "market" / "_provenance"
    pdir.mkdir(parents=True, exist_ok=True)
    totals = {"rows": 0, "padding_dropped": 0, "months": 0, "blocked_months": [], "parquet_bytes": 0, "unexplained_missing_bars": 0}
    norm_total = {"rows_in": 0, "rows_dropped": 0, "dropped_weekly_closure": 0, "dropped_daily_break": 0, "dropped_long_dead_runs": 0,
                  "closed_nonflat_rows_kept": 0, "open_hours_flat_rows_kept": 0}
    yearly: dict[int, dict] = {}
    t_write = t_qual = 0.0
    now_iso = datetime.now(timezone.utc).isoformat()
    loop_start = time.perf_counter()
    for year, kept, st in prov.iter_years():
        tic = time.perf_counter()
        ys, ye = pd.Timestamp(year, 1, 1, tz="UTC"), pd.Timestamp(year + 1, 1, 1, tz="UTC")
        files = store.write_bars(kept, merge=True, replace_range=(ys, ye))
        t_write += time.perf_counter() - tic
        for k in norm_total:
            norm_total[k] += st.get(k, 0)
        totals["rows"] += len(kept)
        totals["padding_dropped"] += st["rows_dropped_year"]
        exp = expected_trading_minutes(year, meta)
        yearly[year] = {"rows": int(len(kept)), "expected_trading_minutes": exp, "coverage_pct": round(100 * len(kept) / exp, 3),
                        "padding_dropped": st["rows_dropped_year"]}
        tic = time.perf_counter()
        for month, part in kept.groupby(kept["timestamp_utc"].dt.month):
            ms = pd.Timestamp(year, month, 1, tz="UTC")
            me = ms + pd.offsets.MonthBegin(1)
            rep = validate_bars(part.reset_index(drop=True), timeframe="M1", max_large_gaps=3)
            gaps = gap_summary(rep)
            in_final = final_start is not None and ms >= final_start
            issues = [(i.code, i.severity, i.category, i.count) for i in rep.issues if not (in_final and i.code in PRICE_STAT_CODES)]
            label = f"{year}-{month:02d}"
            prec = {"provider": provider_name, "instrument": instrument, "timeframe": "M1", "month": label, "imported_at": now_iso,
                    "source_dir": str(source_dir), "source_files": [s.path.name for s in sources.values()],
                    "source_sha256": {s.side: s.sha256 for s in sources.values()}, "requested_range": [ms.isoformat(), me.isoformat()],
                    "actual_range": [str(part["timestamp_utc"].min()), str(part["timestamp_utc"].max())], "row_count": int(len(part)),
                    "canonical_data_version": MarketStore.data_version(part), "normalization_version": NORMALIZATION_VERSION,
                    "quality_version": QUALITY_VERSION, "quality": {"passed": rep.passed, "gap_categories": gaps, "issues": issues,
                    "price_statistics_withheld": in_final}}
            (pdir / f"{provider_name}_{instrument}_M1_{label}.json").write_text(json.dumps(prec, indent=1, default=str), encoding="utf-8")
            totals["months"] += 1
            totals["unexplained_missing_bars"] += sum(i.count for i in rep.issues if i.code == "MISSING_BARS")
            if not rep.passed:
                totals["blocked_months"].append({"month": label, "codes": sorted(rep.codes("BLOCK"))})
        t_qual += time.perf_counter() - tic
        log(f"{year}: rows={len(kept)} coverage={yearly[year]['coverage_pct']}% padding_dropped={st['rows_dropped_year']}")
    timings["normalize_padding_s"] = round(time.perf_counter() - loop_start - t_write - t_qual, 2)
    timings["parquet_write_s"] = round(t_write, 2)
    timings["monthly_quality_s"] = round(t_qual, 2)
    totals["parquet_bytes"] = sum(f.stat().st_size for f in store.files(provider_name, instrument, "M1"))
    tic = time.perf_counter()
    register_duckdb(root, provider_name, instrument)
    timings["duckdb_register_s"] = round(time.perf_counter() - tic, 2)

    tic = time.perf_counter()
    ts = store.read_timestamps(provider_name, instrument)
    audit = audit_calendar(ts, instrument)
    timings["calendar_audit_s"] = round(time.perf_counter() - tic, 2)
    tic = time.perf_counter()
    pre_end = final_start if final_start is not None else ts.max() + pd.Timedelta(minutes=1)
    spreads = spread_study(store, provider_name, instrument, pre_end)
    timings["duckdb_spread_scan_s"] = round(time.perf_counter() - tic, 2)
    xcheck = cross_check_online(root, store, instrument, provider_name)
    n_both = max(xcheck["rows_both"], 1)
    xcheck["price_mismatch_rate"] = xcheck["price_mismatch_rows"] / n_both
    xcheck["row_presence_mismatch_rate"] = (xcheck["only_local"] + xcheck["only_online"]) / n_both
    EQUIV_TOL = 1e-4   # 0.01%: the two feeds are the same source; residual rows are local flat/zero-volume stretches vs online ticks
    equivalent = (xcheck["months_compared"] > 0 and xcheck["price_mismatch_rate"] <= EQUIV_TOL and xcheck["row_presence_mismatch_rate"] <= EQUIV_TOL)
    # Months outside the research windows (before split data_start) are reported but do not decide the research verdict.
    range_start = pd.Timestamp(split["development"][0]) if split else None
    in_range = [b for b in totals["blocked_months"] if range_start is None or pd.Timestamp(b["month"] + "-01", tz="UTC") >= range_start]
    out_range = [b for b in totals["blocked_months"] if b not in in_range]

    dataset = {
        "provider": provider_name, "instrument": instrument, "imported_at": now_iso, "source_dir": str(source_dir),
        "source": prov.describe_source(instrument), "join": {k: v for k, v in vars(j).items()},
        "normalization": {**norm_total, "version": NORMALIZATION_VERSION},
        "canonical_rows": totals["rows"], "first_ts": str(ts.min()), "last_ts": str(ts.max()), "months": totals["months"],
        "parquet_bytes": totals["parquet_bytes"], "source_bytes": sum(s.size for s in sources.values()),
        "blocked_months": totals["blocked_months"], "blocked_months_in_research_range": in_range,
        "blocked_months_before_research_range": out_range, "yearly_coverage": yearly, "spread_study": spreads,
        "calendar_audit_summary": {k: audit[k] for k in ("gap_counts", "provider_gap_total_missing_bars", "provider_gap_fraction", "weekly_closure_edges")},
        "cross_check_vs_online_dukascopy": xcheck, "equivalent_to_online_dukascopy": equivalent, "timings": timings,
        "verdict": "BLOCK" if (in_range or j.malformed_bid or j.malformed_ask or j.duplicate_bid or j.duplicate_ask) else
                   ("WARN" if (out_range or j.bid_only or j.ask_only or j.bid_gt_ask or audit["gap_counts"]["PROVIDER_GAP"]
                               or spreads["abnormal_gt_8x_median"]) else "PASS")}
    rep_dir = root / "reports"
    rep_dir.mkdir(parents=True, exist_ok=True)
    (rep_dir / f"{provider_name}_{instrument}_import_report.json").write_text(json.dumps(dataset, indent=1, default=str), encoding="utf-8")
    (rep_dir / f"calendar_audit_{instrument}.json").write_text(json.dumps(audit, indent=1, default=str), encoding="utf-8")
    (pdir / f"{provider_name}_{instrument}_M1_dataset.json").write_text(json.dumps(dataset, indent=1, default=str), encoding="utf-8")
    if equivalent and dataset["verdict"] != "BLOCK":
        dataset["alias_written"] = True
        alias = {"to": provider_name, "reason": "local export matches the Dukascopy feed on overlapping complete months (price mismatch rate %.5f%%, row-presence mismatch "
                 "rate %.5f%%); lets a spec frozen with provider='dukascopy' resolve to the validated local history without mutating the spec"
                 % (100 * xcheck["price_mismatch_rate"], 100 * xcheck["row_presence_mismatch_rate"]), "verified_equivalent": xcheck,
                 "created_at": now_iso}
        ap = root / "experiments" / "provider_aliases.json"
        doc = json.loads(ap.read_text(encoding="utf-8")) if ap.exists() else {"aliases": {}}
        doc["aliases"].setdefault(instrument.upper(), {})["dukascopy"] = alias
        ap.write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")
    timings["total_s"] = round(time.perf_counter() - t_all, 2)
    prov.close()
    return dataset


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source_dir")
    ap.add_argument("instrument")
    a = ap.parse_args(argv)
    d = import_local(a.source_dir, a.instrument.upper(), log=lambda m: print(m, flush=True))
    print(json.dumps({k: d[k] for k in ("verdict", "canonical_rows", "first_ts", "last_ts", "months", "parquet_bytes", "source_bytes",
                                         "blocked_months_in_research_range", "blocked_months_before_research_range", "equivalent_to_online_dukascopy", "timings")}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
