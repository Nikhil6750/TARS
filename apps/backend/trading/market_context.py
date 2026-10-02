"""MarketContext -- the one compact, cross-source market-data snapshot
(MT5 + TradingView + Calendar + News) for a symbol, per the mission's
section 8 schema.

Deliberately separate from `trading.context.TradingContext` (unchanged,
strategy/active-setups-focused -- `StrategyProvider` is a different, not-
yet-configured boundary). This is the live-market-data combination the
mission's own example describes, assembled entirely from EXISTING monitor
state (`MonitorManager`, `TradingViewAdapter`) -- never a new polling loop,
never a new data source, and never a giant history (mission: "Do NOT put
giant histories in this object" -- calendar/news are capped to a few
genuinely relevant rows each).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class MarketContext:
    symbol: str
    timeframe: str | None
    tradingview: dict[str, Any]
    mt5: dict[str, Any]
    calendar: list[dict[str, Any]]
    news: list[dict[str, Any]]
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def as_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "timeframe": self.timeframe, "tradingview": self.tradingview,
                "mt5": self.mt5, "calendar": self.calendar, "news": self.news, "generated_at": self.generated_at}

    def as_evidence_text(self) -> str:
        """Compact plain-text evidence package for one Claude call -- every
        line traces back to a field already on this object, nothing added."""
        lines = [f"Symbol: {self.symbol}" + (f" (currently viewed on {self.timeframe})" if self.timeframe else "")]

        tv = self.tradingview
        if tv.get("monitoring"):
            lines.append(f"TradingView: actively watching {tv.get('symbol') or self.symbol} "
                        f"{tv.get('timeframe') or ''}, chart freshness {tv.get('freshness', 'unknown')}.")
        else:
            lines.append("TradingView: not currently monitored (no fresh chart read available).")

        mt5 = self.mt5
        if mt5.get("state") == "CONNECTED":
            if mt5.get("bid") is not None and mt5.get("ask") is not None:
                lines.append(f"MT5 live quote: bid {mt5['bid']} / ask {mt5['ask']} (spread {mt5.get('spread')}).")
            else:
                lines.append(f"MT5: connected, but no live quote for {self.symbol}.")
            if mt5.get("exposure"):
                for p in mt5["exposure"]:
                    lines.append(f"Open position: {p.get('side')} {p.get('volume')} lots at {p.get('entry')}, "
                                f"current {p.get('current_price')}, floating P&L {p.get('profit')}.")
            else:
                lines.append("No open position on this symbol.")
        else:
            lines.append(f"MT5: {mt5.get('state', 'unknown')} ({mt5.get('detail', 'no detail')}).")

        if self.calendar:
            for e in self.calendar:
                lines.append(f"Calendar: {e.get('currency')} {e.get('event')} at {e.get('at')} "
                            f"({e.get('importance', 'unknown')} importance).")
        else:
            lines.append("No important calendar events in the relevant window.")

        if self.news:
            for n in self.news:
                lines.append(f"News: {n.get('headline')} ({n.get('source', 'unknown source')}).")
        else:
            lines.append("No recent significant news for this symbol.")

        return "\n".join(lines)


async def build_market_context(monitors, tradingview_adapter, symbol: str) -> MarketContext:
    """Assembles the snapshot for `symbol` purely from already-running
    monitor state (one `monitors.status()` read, one adapter read, one
    news read) -- no new network calls, no new polling."""
    symbol = (symbol or "EURUSD").strip().upper()
    status = await monitors.status()

    mt5_snap = status["mt5"]
    quote = mt5_snap.get("quotes", {}).get(symbol)
    exposure = [p for p in mt5_snap.get("positions", []) if p.get("symbol") == symbol]
    mt5_block = {
        "state": mt5_snap.get("state"), "detail": mt5_snap.get("detail"),
        "bid": quote.get("bid") if quote else None, "ask": quote.get("ask") if quote else None,
        "spread": quote.get("spread") if quote else None,
        "exposure": exposure, "floating_pnl": mt5_snap.get("floating_pnl"),
    }

    tv_block = await tradingview_adapter.monitor_chart()

    calendar_snap = status.get("calendar") or {}
    calendar_events = [calendar_snap["next"]] if calendar_snap.get("next") else []

    news_rows = monitors.news.latest(symbols=[symbol], limit=5) if getattr(monitors, "news", None) else []
    news_block = [
        {"headline": n["headline"], "source": n.get("source"), "url": n.get("url"),
         "published_at": n["published_at"].isoformat() if hasattr(n.get("published_at"), "isoformat") else n.get("published_at")}
        for n in news_rows
    ]

    return MarketContext(symbol=symbol, timeframe=tv_block.get("timeframe"),
                         tradingview=tv_block, mt5=mt5_block, calendar=calendar_events, news=news_block)
