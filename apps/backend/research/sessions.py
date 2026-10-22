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


def easter_sunday(year: int) -> "pd.Timestamp":
    """Anonymous Gregorian algorithm (Meeus/Jones/Butcher)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return pd.Timestamp(year, month, day + 1)


def holiday_calendar(year_from: int, year_to: int) -> dict:
    """UTC calendar dates with their class. FULL = liquidity is normally absent/closed all day (Dec 25, Jan 1, Good Friday);
    SHORT_CANDIDATE = commonly shortened/thin (Dec 24, Dec 26, Dec 31, Easter Monday) but NOT assumed closed: gaps there stay
    warnings unless proven otherwise."""
    out: dict = {}
    for y in range(year_from, year_to + 1):
        out[pd.Timestamp(y, 12, 25).date()] = ("Christmas Day", "FULL")
        out[pd.Timestamp(y, 1, 1).date()] = ("New Year's Day", "FULL")
        e = easter_sunday(y)
        out[(e - pd.Timedelta(days=2)).date()] = ("Good Friday", "FULL")
        out[(e + pd.Timedelta(days=1)).date()] = ("Easter Monday", "SHORT_CANDIDATE")
        for md, name in (((12, 24), "Christmas Eve"), ((12, 26), "Boxing Day"), ((12, 31), "New Year's Eve")):
            out[pd.Timestamp(y, *md).date()] = (name, "SHORT_CANDIDATE")
    return out


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> "pd.Timestamp":
    """n-th (1-based; -1 = last) given weekday (0=Mon) of a month."""
    if n > 0:
        first = pd.Timestamp(year, month, 1)
        return first + pd.Timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = pd.Timestamp(year, month, 1) + pd.offsets.MonthEnd(0)
    return last - pd.Timedelta(days=(last.weekday() - weekday) % 7)


def us_reduced_hours_dates(year_from: int, year_to: int) -> dict:
    """US holidays on which COMEX-linked spot metals show a reduced-hours halt in the New York afternoon (observed in the data as
    ~13:00-18:00 ET gaps on MLK Day and Presidents' Day). Fixed-date holidays use their calendar date (no weekend-observance shifts)."""
    out: dict = {}
    for y in range(year_from, year_to + 1):
        out[_nth_weekday(y, 1, 0, 3).date()] = "Martin Luther King Jr. Day"
        out[_nth_weekday(y, 2, 0, 3).date()] = "Presidents' Day"
        out[_nth_weekday(y, 5, 0, -1).date()] = "Memorial Day"
        out[pd.Timestamp(y, 6, 19).date()] = "Juneteenth"
        out[pd.Timestamp(y, 7, 4).date()] = "Independence Day"
        out[_nth_weekday(y, 9, 0, 1).date()] = "Labor Day"
        out[_nth_weekday(y, 11, 3, 4).date()] = "Thanksgiving Day"
    return out


def is_known_holiday(ts) -> pd.Series:
    """FULL-closure holiday dates only (Christmas Day, New Year's Day, Good Friday)."""
    s = _as_utc_series(ts)
    years = s.dt.year
    cal = holiday_calendar(int(years.min()), int(years.max())) if len(s) else {}
    full = {d for d, (_, kind) in cal.items() if kind == "FULL"}
    return s.dt.date.map(lambda d: d in full)


def explain_gap(start: "pd.Timestamp", end_exclusive: "pd.Timestamp", meta: InstrumentMeta, *, tolerance_min: int = 120,
                max_scan_days: int = 14, short_max_hours: float = 8.0) -> dict:
    """Explain a missing interval [start, end_exclusive) minute by minute. A minute is *explained* only if it is inside the
    weekly closure, the daily break, or a FULL-closure holiday date (UTC). Everything else is unexplained. Unknown stays unknown.

    category: MARKET_CLOSED | EXPECTED_GAP | HOLIDAY_WINDOW | UNVERIFIED_SHORT | PROVIDER_GAP | NOT_INGESTED
    unexplained_minutes is the size of the data-quality problem (0 for fully explained gaps)."""
    span_min = int((end_exclusive - start) / pd.Timedelta(minutes=1))
    if span_min > max_scan_days * 1440:
        return {"category": "NOT_INGESTED", "unexplained_minutes": span_min, "minutes": span_min}
    mins = pd.Series(pd.date_range(start, periods=span_min, freq="1min"))
    closed = is_market_closed(mins, meta).to_numpy()
    brk = is_daily_break(mins, meta).to_numpy()
    cal = holiday_calendar(int(mins.dt.year.min()), int(mins.dt.year.max()))
    dates = mins.dt.date
    full = dates.map(lambda d: cal.get(d, (None, None))[1] == "FULL").to_numpy()
    short = dates.map(lambda d: cal.get(d, (None, None))[1] == "SHORT_CANDIDATE").to_numpy()
    # US holiday reduced hours: ONLY the 12:00-18:00 New York window on those dates is explained (pattern-matched, not assumed)
    local = mins.dt.tz_localize("UTC").dt.tz_convert(meta.timezone) if mins.dt.tz is None else mins.dt.tz_convert(meta.timezone)
    us_dates = us_reduced_hours_dates(int(local.dt.year.min()), int(local.dt.year.max()))
    lmin = (local.dt.hour * 60 + local.dt.minute).to_numpy()
    us = local.dt.date.map(lambda d: d in us_dates).to_numpy() & (lmin >= 12 * 60) & (lmin < 18 * 60)
    explained = closed | brk | full | us
    unexplained = int((~explained).sum())
    short_only = int((~explained & short).sum())
    names = sorted({cal[d][0] for d in set(dates[full]) if d in cal})
    need_holiday = (~(closed | brk)) & full          # minutes that ONLY the holiday rule explains
    need_us = (~(closed | brk | full)) & us
    if need_us.any():
        names = sorted(set(names) | {us_dates[d] for d in set(local.dt.date[need_us])})
    if unexplained == 0:
        if need_holiday.any() or need_us.any():
            cat = "HOLIDAY_WINDOW"
        else:
            cat = "MARKET_CLOSED" if closed.any() else "EXPECTED_GAP"
    elif unexplained == short_only and unexplained <= short_max_hours * 60:
        cat = "UNVERIFIED_SHORT"
    elif unexplained <= tolerance_min and (need_holiday.any() or need_us.any()):
        cat = "HOLIDAY_WINDOW"          # holiday boundary minutes (UTC date vs venue rollover)
    else:
        cat = "PROVIDER_GAP"
    return {"category": cat, "unexplained_minutes": unexplained, "minutes": span_min, "holidays": names,
            "closed_minutes": int(closed.sum()), "break_minutes": int(brk.sum()), "holiday_minutes": int(full.sum()), "us_reduced_minutes": int(us.sum())}
