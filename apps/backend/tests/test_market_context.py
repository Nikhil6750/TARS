"""MarketContext -- the unified MT5+TradingView+Calendar+News snapshot
(mission section 8). Built from fakes shaped exactly like
`MonitorManager.status()`/`TradingViewAdapter.monitor_chart()`/
`NewsMonitor.latest()` already return (see their own test files for those
individually) -- these tests verify the assembly/formatting logic only.
"""
from __future__ import annotations

from datetime import UTC, datetime

from trading.market_context import MarketContext, build_market_context


class _FakeMonitors:
    def __init__(self, status: dict, news_rows: list[dict] | None = None):
        self._status = status
        self.news = _FakeNews(news_rows or []) if news_rows is not None else None

    async def status(self):
        return self._status


class _FakeNews:
    def __init__(self, rows):
        self._rows = rows

    def latest(self, symbols=None, limit=10):
        if symbols:
            wanted = set(symbols)
            return [r for r in self._rows if r.get("symbol") in wanted][:limit]
        return self._rows[:limit]


class _FakeAdapter:
    def __init__(self, monitor_result: dict):
        self._result = monitor_result

    async def monitor_chart(self):
        return self._result


def _status(mt5_state="CONNECTED", quotes=None, positions=None, calendar_next=None):
    return {
        "mt5": {"state": mt5_state, "detail": "ok", "quotes": quotes or {}, "positions": positions or [],
                "floating_pnl": sum(p.get("profit", 0) for p in (positions or []))},
        "calendar": {"next": calendar_next},
    }


async def test_build_market_context_combines_all_four_sources():
    status = _status(
        quotes={"EURUSD": {"bid": 1.0840, "ask": 1.0842, "spread": 2.0}},
        positions=[{"symbol": "EURUSD", "side": "BUY", "volume": 0.1, "entry": 1.08, "current_price": 1.084, "profit": 4.0}],
        calendar_next={"currency": "USD", "event": "CPI", "at": "2026-01-01T13:30:00+00:00", "importance": "High"},
    )
    monitors = _FakeMonitors(status, news_rows=[{"headline": "Fed holds rates", "source": "rss", "url": "https://x",
                                                 "symbol": "EURUSD", "published_at": datetime.now(UTC)}])
    adapter = _FakeAdapter({"monitoring": True, "symbol": "EURUSD", "timeframe": "15m", "freshness": "hot"})

    ctx = await build_market_context(monitors, adapter, "eurusd")

    assert ctx.symbol == "EURUSD"
    assert ctx.timeframe == "15m"
    assert ctx.tradingview["monitoring"] is True
    assert ctx.mt5["bid"] == 1.0840 and ctx.mt5["ask"] == 1.0842
    assert ctx.mt5["exposure"][0]["profit"] == 4.0
    assert ctx.calendar == [{"currency": "USD", "event": "CPI", "at": "2026-01-01T13:30:00+00:00", "importance": "High"}]
    assert len(ctx.news) == 1 and ctx.news[0]["headline"] == "Fed holds rates"


async def test_build_market_context_without_quote_or_position_or_news():
    status = _status(mt5_state="DISCONNECTED_AUTH_REQUIRED")
    monitors = _FakeMonitors(status, news_rows=[])
    adapter = _FakeAdapter({"monitoring": False, "symbol": None, "timeframe": None})

    ctx = await build_market_context(monitors, adapter, "XAUUSD")

    assert ctx.mt5["state"] == "DISCONNECTED_AUTH_REQUIRED"
    assert ctx.mt5["bid"] is None and ctx.mt5["ask"] is None
    assert ctx.mt5["exposure"] == []
    assert ctx.calendar == []
    assert ctx.news == []


async def test_build_market_context_without_news_monitor():
    status = _status()
    monitors = _FakeMonitors(status, news_rows=None)  # monitors.news is None entirely
    adapter = _FakeAdapter({"monitoring": False})
    ctx = await build_market_context(monitors, adapter, "EURUSD")
    assert ctx.news == []


def test_as_dict_round_trips_all_fields():
    ctx = MarketContext(symbol="EURUSD", timeframe="15m", tradingview={"monitoring": True},
                        mt5={"state": "CONNECTED"}, calendar=[], news=[])
    d = ctx.as_dict()
    assert d["symbol"] == "EURUSD" and d["timeframe"] == "15m"
    assert "generated_at" in d


def test_as_evidence_text_is_compact_and_grounded():
    ctx = MarketContext(
        symbol="XAUUSD", timeframe="15m",
        tradingview={"monitoring": True, "symbol": "XAUUSD", "timeframe": "15m", "freshness": "hot"},
        mt5={"state": "CONNECTED", "bid": 2650.0, "ask": 2650.5, "spread": 5,
             "exposure": [{"side": "BUY", "volume": 0.5, "entry": 2600.0, "current_price": 2650.0, "profit": 2500.0}],
             "floating_pnl": 2500.0},
        calendar=[{"currency": "USD", "event": "CPI", "at": "2026-01-01T13:30:00+00:00", "importance": "High"}],
        news=[{"headline": "Gold hits record high", "source": "rss"}],
    )
    text = ctx.as_evidence_text()
    assert "XAUUSD" in text
    assert "2650.0" in text and "2650.5" in text
    assert "BUY" in text and "2500.0" in text
    assert "CPI" in text
    assert "Gold hits record high" in text
    # Never a giant history: a handful of short lines, not paragraphs.
    assert len(text.splitlines()) < 10


def test_as_evidence_text_honest_about_absence():
    ctx = MarketContext(symbol="EURUSD", timeframe=None, tradingview={"monitoring": False},
                        mt5={"state": "DISCONNECTED_AUTH_REQUIRED", "detail": "not authenticated"},
                        calendar=[], news=[])
    text = ctx.as_evidence_text()
    assert "not currently monitored" in text
    assert "DISCONNECTED_AUTH_REQUIRED" in text
    assert "No important calendar events" in text
    assert "No recent significant news" in text
