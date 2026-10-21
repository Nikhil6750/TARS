"""Local market-state intelligence: statistically meaningful change detection with NO Claude call per candle.
Reuses the existing watcher's escalation gate (trading.market_watch_service.requires_reasoning / EventCandidate)
so there is a single definition of "deserves reasoning". STALE/UNAVAILABLE chart evidence is never current."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from trading.market_watch_service import EventCandidate, requires_reasoning

from .backtest import session_of
from .schema import TIMEFRAME_MINUTES, pip_size

EVENT_WINDOW_MIN = 30
ABNORMAL_Z = 3.0


@dataclass
class MarketState:
    instrument: str
    timestamp: str
    returns: dict
    rolling_volatility: float | None
    volatility_percentile: float | None
    abnormal_move_score: float | None
    spread_state: str
    spread_pips: float | None
    session: str
    regime: dict
    event_proximity: dict
    recent_events: list
    cross_asset: dict
    data_freshness: str
    chart_evidence: str
    significance: float
    confidence: float
    requires_reasoning: bool
    escalation_reason: str
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _freshness(last_ts: pd.Timestamp | None, now: datetime, tf_minutes: int) -> str:
    if last_ts is None:
        return "UNAVAILABLE"
    age = (now - last_ts.to_pydatetime()).total_seconds() / 60
    return "FRESH" if age <= 2 * tf_minutes else "STALE"


def compute_market_state(bars: pd.DataFrame, *, now: datetime | None = None, events: list[dict] | None = None,
                         others: dict[str, pd.DataFrame] | None = None, chart_evidence: str = "UNAVAILABLE",
                         has_open_position: bool = False, significance_threshold: float = 0.3) -> MarketState:
    """`bars`: canonical bars (bid/ask) for one instrument at its source timeframe, oldest first.
    `events`: [{title, at (UTC datetime), impact}]. `chart_evidence`: CURRENT | STALE | UNAVAILABLE."""
    now = now or datetime.now(timezone.utc)
    inst = str(bars["instrument"].iloc[-1]) if len(bars) else "?"
    tf_min = TIMEFRAME_MINUTES.get(str(bars["source_timeframe"].iloc[-1]) if len(bars) else "M1", 1)
    notes: list[str] = []
    if len(bars) == 0:
        return MarketState(inst, now.isoformat(), {}, None, None, None, "UNKNOWN", None, session_of(now.hour), {}, {}, [], {},
                           "UNAVAILABLE", "UNAVAILABLE", 0.0, 0.0, False, "no data", ["no bars"])
    last_ts = bars["timestamp_utc"].iloc[-1]
    fresh = _freshness(last_ts, now, tf_min)
    mid = ((bars["bid_close"] + bars["ask_close"]) / 2).astype(float)
    lr = np.log(mid).diff()
    returns = {}
    for k in (1, 5, 15, 60):
        returns[f"{k}bar"] = float(np.log(mid.iloc[-1] / mid.iloc[-1 - k])) if len(mid) > k else None
    win = min(60, max(len(lr) - 1, 2))
    vol_series = lr.rolling(win, min_periods=max(win // 2, 5)).std()
    vol = float(vol_series.iloc[-1]) if pd.notna(vol_series.iloc[-1]) else None
    pct = None
    hist = vol_series.dropna().iloc[-2000:]
    if vol is not None and len(hist) >= 50:
        pct = float((hist <= vol).mean())
    z = None
    nz = 5
    if vol and vol > 0 and len(mid) > nz:
        z = float(abs(np.log(mid.iloc[-1] / mid.iloc[-1 - nz])) / (vol * np.sqrt(nz)))
    spreads = (bars["ask_close"] - bars["bid_close"]).astype(float)
    med = float(spreads.iloc[-500:].median()) if len(spreads) >= 20 else None
    cur = float(spreads.iloc[-1])
    sstate = "UNKNOWN" if not med or med <= 0 else ("EXTREME" if cur > 3 * med else "WIDE" if cur > 1.5 * med else "NORMAL")
    er = None
    if len(mid) > 30:
        d = mid.diff().abs().iloc[-30:].sum()
        er = float(abs(mid.iloc[-1] - mid.iloc[-31]) / d) if d > 0 else None
    regime = {"trend_range": None if er is None else ("TREND" if er > 0.3 else "RANGE"), "efficiency_ratio": er,
              "volatility": None if pct is None else ("HIGH" if pct > 2 / 3 else "LOW" if pct < 1 / 3 else "MID")}

    near, recent = {}, []
    nearest = None
    for ev in events or []:
        dt = (ev["at"] - now).total_seconds() / 60
        recent_ev = {"title": ev.get("title"), "minutes_from_now": round(dt, 1), "impact": ev.get("impact")}
        if abs(dt) <= 24 * 60:
            recent.append(recent_ev)
        if nearest is None or abs(dt) < abs(nearest["minutes_from_now"]):
            nearest = recent_ev
    near = nearest or {}
    event_in_window = bool(nearest and abs(nearest["minutes_from_now"]) <= EVENT_WINDOW_MIN)

    cross = {}
    for name, ob in (others or {}).items():
        if ob is None or len(ob) < 20:
            continue
        m2 = ((ob["bid_close"] + ob["ask_close"]) / 2).astype(float)
        r2 = np.log(m2).diff()
        s2 = r2.iloc[-60:].std()
        if s2 and s2 > 0:
            cross[name] = {"abnormal_move_score": float(abs(np.log(m2.iloc[-1] / m2.iloc[-6])) / (s2 * np.sqrt(5)))}
    cross_confirm = any(v["abnormal_move_score"] >= ABNORMAL_Z for v in cross.values())

    chart = chart_evidence if chart_evidence in {"CURRENT", "STALE", "UNAVAILABLE"} else "UNAVAILABLE"
    if chart != "CURRENT":
        notes.append(f"chart evidence {chart}: excluded from current evidence")

    sig = 0.0
    if z is not None:
        sig += 0.5 * min(z / 4.0, 1.0)
    if pct is not None:
        sig += 0.15 * max(pct - 0.5, 0) * 2
    if event_in_window:
        sig += 0.2
    if sstate in ("WIDE", "EXTREME"):
        sig += 0.05 if sstate == "WIDE" else 0.1
    if cross_confirm:
        sig += 0.1
    sig = float(min(sig, 1.0))
    conf = 1.0
    if fresh != "FRESH":
        conf *= 0.2
    if pct is None or z is None:
        conf *= 0.5
    if len(bars) < 200:
        conf *= 0.6

    cand = EventCandidate(
        symbol=inst, has_price_move=bool(z is not None and z >= ABNORMAL_Z), move_pct=returns.get("5bar"),
        move_is_unusual_without_obvious_cause=bool(z is not None and z >= ABNORMAL_Z and not event_in_window),
        insufficient_price_history=len(bars) < 200, has_calendar_event=event_in_window,
        calendar_title=(nearest or {}).get("title") or "", has_open_position=has_open_position)
    gate = requires_reasoning(cand)
    if fresh != "FRESH":
        need, reason = False, f"data {fresh}: cannot reason on stale/unavailable data"
    elif gate and sig >= significance_threshold:
        need, reason = True, "watcher gate passed, data fresh, significance above noise floor"
    else:
        need, reason = False, "below escalation threshold" if not gate else "gate passed but significance low"
    return MarketState(inst, now.isoformat(), returns, vol, pct, z, sstate, cur / pip_size(inst), session_of(now.hour), regime,
                       near, recent, cross, fresh, chart, sig, float(conf), need, reason, notes)


class MarketIntelligenceEngine:
    """Holds the latest local state per instrument; Claude is consulted only for states that requires_reasoning."""

    def __init__(self) -> None:
        self.states: dict[str, MarketState] = {}

    def update(self, instrument: str, bars: pd.DataFrame, **kw) -> MarketState:
        st = compute_market_state(bars, **kw)
        self.states[instrument.upper()] = st
        return st

    def escalations(self) -> list[MarketState]:
        return [s for s in self.states.values() if s.requires_reasoning]
