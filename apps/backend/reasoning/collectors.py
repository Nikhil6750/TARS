"""Evidence collectors for the RESEARCH path. Each returns one EvidenceItem with an honest as_of timestamp and a freshness computed from
that timestamp - a failure becomes UNAVAILABLE evidence, never a guess. Collectors are independent so the pipeline runs them in parallel."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import httpx

from .review import EvidenceItem

QUOTE_URL = "https://api.gold-api.com/price/{sym}"
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
GOLD_API_SYMBOL = {"XAUUSD": "XAU", "XAGUSD": "XAG", "BTCUSD": "BTC"}
QUOTE_MAX_AGE_S = 180
_INSTRUMENTS = (("XAUUSD", r"\b(gold|xau\w*)\b"), ("XAGUSD", r"\b(silver|xag\w*)\b"), ("BTCUSD", r"\b(bitcoin|btc\w*)\b"),
                ("EURUSD", r"\b(euro|eurusd)\b"), ("GBPUSD", r"\b(pound|cable|gbpusd)\b"), ("USDJPY", r"\b(yen|usdjpy)\b"))


def detect_instrument(text: str) -> str | None:
    low = (text or "").lower()
    return next((sym for sym, rx in _INSTRUMENTS if re.search(rx, low)), None)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat()


async def public_quote(symbol: str, *, client: httpx.AsyncClient | None = None, now: datetime | None = None) -> EvidenceItem:
    """Last spot price from a public quote API. ONE price + its own timestamp: no bid/ask, no history, no volume (stated in the provenance)."""
    now = now or datetime.now(timezone.utc)
    prov = "api.gold-api.com public spot quote: single last price with provider timestamp; no bid/ask, spread, volume or history"
    api_sym = GOLD_API_SYMBOL.get(symbol)
    if api_sym is None:
        return EvidenceItem(name=f"quote_{symbol}", kind="market", freshness="UNAVAILABLE", provenance=prov, summary=f"no live quote source is configured for {symbol}")
    try:
        async with (client or httpx.AsyncClient(timeout=8)) as c:
            r = await c.get(QUOTE_URL.format(sym=api_sym), headers={"User-Agent": "TARS/1.0"})
        r.raise_for_status()
        d = r.json()
        updated = datetime.fromisoformat(d["updatedAt"].replace("Z", "+00:00"))
        age = (now - updated).total_seconds()
        fresh = "CURRENT" if age <= QUOTE_MAX_AGE_S else "STALE"
        return EvidenceItem(name=f"quote_{symbol}", kind="market", as_of=_iso(updated), freshness=fresh, provenance=prov,
                            summary=f"{symbol} last {float(d['price']):.2f} USD at {_iso(updated)} ({age:.0f}s old)")
    except Exception as exc:
        return EvidenceItem(name=f"quote_{symbol}", kind="market", freshness="UNAVAILABLE", provenance=prov, summary=f"quote fetch failed: {type(exc).__name__}")


async def upcoming_events(currency: str = "USD", *, hours: int = 24, client: httpx.AsyncClient | None = None, now: datetime | None = None) -> EvidenceItem:
    now = now or datetime.now(timezone.utc)
    prov = "faireconomy weekly economic calendar (this week's feed, fetched now)"
    try:
        async with (client or httpx.AsyncClient(timeout=10)) as c:
            r = await c.get(CALENDAR_URL, headers={"User-Agent": "TARS/1.0"})
        r.raise_for_status()
        rows = r.json()
        picked = []
        for ev in rows:
            if ev.get("country") != currency or ev.get("impact") not in ("High",):
                continue
            t = datetime.fromisoformat(ev["date"]).astimezone(timezone.utc)
            if now - timedelta(hours=2) <= t <= now + timedelta(hours=hours):
                picked.append((t, ev))
        picked.sort(key=lambda x: x[0])
        if picked:
            text = "; ".join(f"{ev['title']} at {_iso(t)}" + (f" (forecast {ev['forecast']})" if ev.get("forecast") else "") for t, ev in picked[:3])
        else:
            text = f"no high-impact {currency} events from 2h ago to {hours}h ahead in this week's feed"
        return EvidenceItem(name=f"calendar_{currency}", kind="event", as_of=_iso(now), freshness="CURRENT", provenance=prov, summary=text[:900])
    except Exception as exc:
        return EvidenceItem(name=f"calendar_{currency}", kind="event", freshness="UNAVAILABLE", provenance=prov, summary=f"calendar fetch failed: {type(exc).__name__}")


_EXPERIMENT = re.compile(r"\bH(\d{4})\b", re.I)


def experiment_collector(registry, agent):
    async def collect(question: str) -> EvidenceItem | None:
        m = _EXPERIMENT.search(question or "")
        if not m:
            return None
        from .evidence import experiment_evidence
        try:
            item, _quant = experiment_evidence(registry, agent, f"H{m.group(1)}")
            return item
        except Exception as exc:
            return EvidenceItem(name=f"experiment_H{m.group(1)}", kind="quant", freshness="UNAVAILABLE", summary=f"experiment evidence unavailable: {type(exc).__name__}")
    return collect


def tars_state_collector(monitors, tradingview_adapter, symbol_hint=lambda q: None):
    """Evidence from TARS's own monitors for a symbol: MT5 quote and the TradingView chart read, each with its OWN freshness. A STALE or
    UNAVAILABLE chart is reported as such (never as current evidence). Returns a list of EvidenceItems; [] when nothing is applicable."""
    async def collect(question: str):
        symbol = symbol_hint(question) or detect_instrument(question)
        if not symbol or monitors is None:
            return []
        from trading.market_context import build_market_context
        try:
            m = await build_market_context(monitors, tradingview_adapter, symbol)
        except Exception as exc:
            return [EvidenceItem(name=f"tars_state_{symbol}", kind="watcher", freshness="UNAVAILABLE", summary=f"TARS state unavailable: {type(exc).__name__}")]
        items = []
        mt5 = m.mt5 or {}
        q = (mt5.get("quotes") or {}).get(symbol)
        if mt5.get("state") == "CONNECTED" and q:
            items.append(EvidenceItem(name=f"mt5_{symbol}", kind="market", as_of=mt5.get("updated_at"), freshness="CURRENT",
                                      provenance="MetaTrader 5 terminal via TARS (read-only)", summary=f"{symbol} bid {q.get('bid')} ask {q.get('ask')}"[:300]))
        else:
            items.append(EvidenceItem(name=f"mt5_{symbol}", kind="market", freshness="UNAVAILABLE", provenance="MetaTrader 5 terminal via TARS (read-only)",
                                      summary=f"no MT5 quote for {symbol} (state {mt5.get('state', 'unknown')})"))
        tv = m.tradingview or {}
        ev = tv.get("evidence", "UNAVAILABLE")
        items.append(EvidenceItem(name=f"tradingview_{symbol}", kind="chart", as_of=m.generated_at if ev == "CURRENT" else None, freshness=ev if ev in ("CURRENT", "STALE", "UNAVAILABLE") else "UNAVAILABLE",
                                  provenance="TradingView chart read via TARS", summary=str(tv.get("detail") or tv.get("state") or "no chart read")[:300]))
        return items
    return collect
