"""Timestamps-only market-calendar audit (no prices are read, so it is safe on the protected FINAL_UNTOUCHED range).

Every gap in the M1 series is classified, never assumed:
  MARKET_CLOSED    weekly closure bounded exactly as the DST-aware rule expects
  EXPECTED_GAP     the instrument's recurring daily break
  HOLIDAY_WINDOW   gap overlapping a FULL-closure holiday (Christmas Day, New Year's Day, Good Friday)
  UNVERIFIED_SHORT gap on a SHORT_CANDIDATE date (Dec 24/26/31, Easter Monday): reported as a WARNING, not assumed closed
  PROVIDER_GAP     everything else - a data-quality warning
Closure edges are also checked against the DST-correct expectation (last bar before / first bar after each weekly closure)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .instruments import InstrumentMeta, get_meta
from .sessions import explain_gap, holiday_calendar
from .storage import MarketStore


def _expected_edges(meta: InstrumentMeta, ts_in_week: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp]:
    """(last bar open before the weekly close, first bar open after the weekly open) for the week containing ts_in_week."""
    local = ts_in_week.tz_convert(meta.timezone)
    monday = (local - pd.Timedelta(days=local.weekday())).normalize()
    cd, ct = meta.week_close_local
    od, ot = meta.week_open_local
    close_local = (monday + pd.Timedelta(days=cd)).replace(hour=ct.hour, minute=ct.minute)
    open_local = (monday + pd.Timedelta(days=od)).replace(hour=ot.hour, minute=ot.minute)
    close_local = close_local.tz_localize(None).tz_localize(meta.timezone, ambiguous="NaT", nonexistent="shift_forward")
    open_local = open_local.tz_localize(None).tz_localize(meta.timezone, ambiguous="NaT", nonexistent="shift_forward")
    return close_local.tz_convert("UTC") - pd.Timedelta(minutes=1), open_local.tz_convert("UTC")


def audit_calendar(ts: pd.Series, instrument: str, *, short_gap_hours: float = 8.0) -> dict:
    meta = get_meta(instrument)
    ts = pd.Series(pd.to_datetime(ts, utc=True)).sort_values().drop_duplicates().reset_index(drop=True)
    if len(ts) < 2:
        return {"instrument": instrument, "rows": len(ts), "gaps": 0}
    mins = ((ts - ts.iloc[0]).dt.total_seconds() // 60).astype("int64").to_numpy()   # unit-safe (DuckDB returns microseconds)
    d = np.diff(mins)
    idx = np.nonzero(d > 1)[0]
    cal = holiday_calendar(int(ts.dt.year.min()), int(ts.dt.year.max()) + 1)
    counts = {"MARKET_CLOSED": 0, "EXPECTED_GAP": 0, "HOLIDAY_WINDOW": 0, "UNVERIFIED_SHORT": 0, "PROVIDER_GAP": 0, "NOT_INGESTED": 0}
    coverage_holes = []
    provider_gaps, holiday_gaps, short_gaps, edge_mismatches = [], [], [], []
    edge_ok = edge_total = 0
    per_year: dict[int, dict] = {}
    for i in idx:
        a, b = ts.iloc[i], ts.iloc[i + 1]
        first_missing, last_missing = a + pd.Timedelta(minutes=1), b - pd.Timedelta(minutes=1)
        span = b - a
        missing = int(d[i] - 1)
        ex = explain_gap(first_missing, b, meta, short_max_hours=short_gap_hours)
        cat = ex["category"]
        dates = {x.date() for x in pd.date_range(first_missing.normalize(), last_missing.normalize(), freq="D")}
        if cat == "MARKET_CLOSED" and span <= pd.Timedelta(hours=72):
            e_close, e_open = _expected_edges(meta, first_missing)
            edge_total += 1
            if a == e_close and b == e_open:
                edge_ok += 1
            elif len(edge_mismatches) < 50:
                edge_mismatches.append({"gap_after": str(a), "resumes": str(b), "expected_last_bar": str(e_close),
                                        "expected_first_bar": str(e_open), "close_offset_min": int((a - e_close).total_seconds() // 60),
                                        "open_offset_min": int((b - e_open).total_seconds() // 60)})
        elif cat == "HOLIDAY_WINDOW":
            holiday_gaps.append({"after": str(a), "resumes": str(b), "missing_bars": missing, "holiday": ex["holidays"],
                                 "unexplained_boundary_minutes": ex["unexplained_minutes"]})
        elif cat == "UNVERIFIED_SHORT":
            short_gaps.append({"after": str(a), "resumes": str(b), "missing_bars": missing,
                               "date": sorted(cal[d][0] for d in dates if d in cal and cal[d][1] == "SHORT_CANDIDATE")})
        elif cat == "PROVIDER_GAP":
            provider_gaps.append({"after": str(a), "resumes": str(b), "missing_bars": ex["unexplained_minutes"],
                                  "hours": round(span.total_seconds() / 3600, 2)})
        elif cat == "NOT_INGESTED":
            coverage_holes.append({"after": str(a), "resumes": str(b), "days": round(span.total_seconds() / 86400, 1)})
        counts[cat] += 1
        y = per_year.setdefault(int(a.year), dict.fromkeys(counts, 0))
        y[cat] += 1
    in_session_rows = int(len(ts))
    provider_missing = int(sum(g["missing_bars"] for g in provider_gaps))   # unexplained minutes only
    return {
        "instrument": meta.instrument, "rows": in_session_rows, "first": str(ts.iloc[0]), "last": str(ts.iloc[-1]),
        "gap_counts": counts, "per_year": per_year,
        "provider_gap_total_missing_bars": provider_missing,
        "provider_gap_fraction": provider_missing / max(in_session_rows + provider_missing, 1),
        "provider_gaps": provider_gaps[:200], "coverage_holes_not_ingested": coverage_holes, "holiday_windows": holiday_gaps[:100], "unverified_short_sessions": short_gaps[:100],
        "weekly_closure_edges": {"checked": edge_total, "exact_match": edge_ok, "mismatches": edge_mismatches},
        "dst_note": "closure/open edges are evaluated in the instrument's own timezone, so US DST shifts are accounted for",
    }


def audit_store(root: Path, provider: str, instrument: str, write: bool = True) -> dict:
    ts = MarketStore(root).read_timestamps(provider, instrument)
    rep = audit_calendar(ts, instrument)
    if write:
        out = Path(root) / "reports" / f"calendar_audit_{instrument.upper()}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    return rep
