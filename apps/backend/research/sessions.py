"""DST-correct trading sessions and market-closure logic. Timestamps are stored in UTC; session membership is decided in
each venue's LOCAL civil time (Europe/London, America/New_York, Asia/Tokyo), so the UTC hours shift automatically at DST changes.

Sessions (local, Mon-Fri in that venue's calendar):
  ASIA     Tokyo    09:00-18:00
  LONDON   London   08:00-16:30
  NEWYORK  New York 08:00-17:00
  LONDON_NY_OVERLAP = LONDON and NEWYORK simultaneously
Priority when several apply: OVERLAP > LONDON > NEWYORK > ASIA; otherwise OFF."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .instruments import InstrumentMeta

LONDON, NEW_YORK, TOKYO = "Europe/London", "America/New_York", "Asia/Tokyo"
_WINDOWS = {"LONDON": (LONDON, 8 * 60, 16 * 60 + 30), "NEWYORK": (NEW_YORK, 8 * 60, 17 * 60), "ASIA": (TOKYO, 9 * 60, 18 * 60)}
SESSION_NAMES = ("ASIA", "LONDON", "LONDON_NY_OVERLAP", "NEWYORK", "OFF")


def _as_utc_series(ts) -> pd.Series:
    s = pd.Series(pd.to_datetime(ts))
    if s.dt.tz is None:
        raise ValueError("timestamps must be timezone-aware (UTC)")
    return s.dt.tz_convert("UTC")


def _in_window(local: pd.Series, lo: int, hi: int) -> pd.Series:
    mins = local.dt.hour * 60 + local.dt.minute
    return (local.dt.dayofweek < 5) & (mins >= lo) & (mins < hi)


def classify_sessions(ts) -> pd.Series:
    """Vectorised session label per UTC timestamp (DST-aware)."""
    s = _as_utc_series(ts)
    flags = {name: _in_window(s.dt.tz_convert(tz), lo, hi) for name, (tz, lo, hi) in _WINDOWS.items()}
    out = np.full(len(s), "OFF", dtype=object)
    out[flags["ASIA"].to_numpy()] = "ASIA"
    out[flags["NEWYORK"].to_numpy()] = "NEWYORK"
    out[flags["LONDON"].to_numpy()] = "LONDON"
    out[(flags["LONDON"] & flags["NEWYORK"]).to_numpy()] = "LONDON_NY_OVERLAP"
    return pd.Series(out, index=s.index)


def session_of(ts) -> str:
    if isinstance(ts, (int, float)):
        raise TypeError("session_of takes a timezone-aware timestamp, not an hour (fixed UTC hours are wrong across DST)")
    return str(classify_sessions([pd.Timestamp(ts)]).iloc[0])


def is_market_closed(ts, meta: InstrumentMeta) -> pd.Series:
    """True where the instrument's weekly closure applies (local to meta.timezone, so DST is handled)."""
    s = _as_utc_series(ts)
    local = s.dt.tz_convert(meta.timezone)
    mins = local.dt.hour * 60 + local.dt.minute
    dow = local.dt.dayofweek
    cd, ct = meta.week_close_local
    od, ot = meta.week_open_local
    close_m, open_m = ct.hour * 60 + ct.minute, ot.hour * 60 + ot.minute
    closed = (dow == 5) | ((dow == cd) & (mins >= close_m)) | ((dow == od) & (mins < open_m))
    return closed


def is_daily_break(ts, meta: InstrumentMeta) -> pd.Series:
    s = _as_utc_series(ts)
    if meta.daily_break_local is None:
        return pd.Series(False, index=s.index)
    local = s.dt.tz_convert(meta.timezone)
    mins = local.dt.hour * 60 + local.dt.minute
    a, b = meta.daily_break_local
    return (mins >= a.hour * 60 + a.minute) & (mins < b.hour * 60 + b.minute) & ~is_market_closed(s, meta)


def is_known_holiday(ts) -> pd.Series:
    """Christmas Day and New Year's Day (thin/closed liquidity on most venues)."""
    s = _as_utc_series(ts)
    return ((s.dt.month == 12) & (s.dt.day == 25)) | ((s.dt.month == 1) & (s.dt.day == 1))
