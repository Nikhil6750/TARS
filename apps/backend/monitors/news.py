"""RSS-based news provider + monitor -- mirrors `monitors/calendar.py`'s shape deliberately, so
the two sources are wired and read the same way.

Live source: a configurable RSS/Atom feed. Default `RssNewsProvider` is generic -- any well-formed
RSS 2.0 feed works (verified live against https://www.fxstreet.com/rss/news, a free, no-key
financial-news feed), so adding or swapping a source is a config change, not new code. This is
deliberately NOT per-site HTML scraping (that is exactly the "brittle" approach the mission asked
to avoid); a source without a feed is a browser-automation problem for a later `BrowserWatcher`,
not this module's.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

from events.core import EventSource, NormalizedEvent
from monitors.calendar import NewsProvider

logger = logging.getLogger("tars.monitors.news")

# Same spirit as calendar.py's CURRENCY_SYMBOLS: headline text -> the symbol(s) it's about.
# Keyword form, since a headline says "gold" or "EUR/USD", never a bare "XAUUSD".
_SYMBOL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "EURUSD": ("eur/usd", "eurusd", "euro"),
    "GBPUSD": ("gbp/usd", "gbpusd", "pound", "sterling", "cable"),
    "USDJPY": ("usd/jpy", "usdjpy", "yen"),
    "XAUUSD": ("xau/usd", "xauusd", "gold"),
    "XAGUSD": ("xag/usd", "xagusd", "silver"),
    "AUDUSD": ("aud/usd", "audusd", "aussie"),
    "USDCAD": ("usd/cad", "usdcad", "loonie"),
    "USDCHF": ("usd/chf", "usdchf"),
    "NZDUSD": ("nzd/usd", "nzdusd", "kiwi"),
    "WTI": ("wti", "crude oil", "oil price"),
    "BTCUSD": ("bitcoin", "btc/usd", "btcusd"),
}
_IMPORTANCE_KEYWORDS = re.compile(
    r"\b(fed|fomc|ecb|boe|boj|rate decision|rate hike|rate cut|intervention|nfp|cpi|inflation|"
    r"recession|crisis|emergency|surge|plunge|crash|surprise)\b", re.IGNORECASE)


def classify_symbols(text: str) -> list[str]:
    low = text.lower()
    return [sym for sym, keywords in _SYMBOL_KEYWORDS.items() if any(kw in low for kw in keywords)]


def _parse_pubdate(raw: str) -> datetime:
    if not raw:
        return datetime.now(UTC)
    try:
        when = parsedate_to_datetime(raw)
        return when if when.tzinfo else when.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


class RssNewsProvider(NewsProvider):
    """Generic RSS 2.0 feed. `latest()` ignores `symbols` (the monitor filters after fetch, same
    division of labor as calendar.py) and always returns everything the feed carries."""

    def __init__(self, url: str, name: str = "live:rss"):
        self.url, self.name = url, name

    async def latest(self, symbols: list[str]) -> list[dict]:
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(self.url, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
        return self.parse(response.text)

    def parse(self, xml_text: str) -> list[dict]:
        try:
            root = ElementTree.fromstring(xml_text)
        except ElementTree.ParseError:
            logger.warning("[news] %s returned unparseable XML", self.name)
            return []
        items = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            link = (item.findtext("link") or "").strip()
            summary = (item.findtext("description") or "").strip()
            when = _parse_pubdate((item.findtext("pubDate") or "").strip())
            key = f"{self.name}|{title}|{link}"
            items.append({
                "id": hashlib.sha1(key.encode()).hexdigest()[:16], "source": self.name,
                "headline": title, "published_at": when, "url": link, "summary": summary[:400],
            })
        return items


class NewsMonitor:
    def __init__(self, provider: NewsProvider, publish, on_state, symbols: list[str], *,
                 tick_seconds: float = 300, now=lambda: datetime.now(UTC)):
        self.provider, self.publish, self.on_state = provider, publish, on_state
        self.symbols = [s.upper() for s in symbols]
        self.tick_seconds, self.now = tick_seconds, now
        self.items: list[dict] = []
        self._seen_ids: set[str] = set()
        self.state, self.detail = "DISCONNECTED", "not started"
        self.fetched_at: datetime | None = None
        self._task: asyncio.Task | None = None

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("news tick failed; monitor remains alive")
            await asyncio.sleep(self.tick_seconds)

    async def tick(self):
        first_fetch = self.fetched_at is None
        try:
            fetched = await self.provider.latest(self.symbols)
        except Exception as exc:
            self.state, self.detail = "ERROR", f"news fetch failed: {type(exc).__name__}"
            await self.on_state(self.state)
            return
        self.fetched_at = self.now()
        self.state, self.detail = "CONNECTED", f"{len(fetched)} headlines ({self.provider.name})"
        await self.on_state(self.state)
        self.items = fetched[:50]

        new_items = [it for it in fetched if it["id"] not in self._seen_ids]
        for it in fetched:
            self._seen_ids.add(it["id"])
        if len(self._seen_ids) > 500:
            self._seen_ids = set(list(self._seen_ids)[-300:])

        if first_fetch:
            return  # existing headlines on startup are not "new" -- never flood the event core
        for it in new_items:
            symbols = classify_symbols(it["headline"] + " " + it.get("summary", ""))
            if self.symbols and not (set(symbols) & set(self.symbols)):
                continue
            important = bool(_IMPORTANCE_KEYWORDS.search(it["headline"]))
            severity = 3 if important else 2
            await self.publish(NormalizedEvent(
                source=EventSource.NEWS, kind="news.headline", severity=severity,
                symbol=symbols[0] if symbols else None, title=it["headline"][:200],
                summary=it.get("summary", "")[:2000], dedupe_key=it["id"],
                expires_at=self.now() + timedelta(hours=2),
                payload={"url": it["url"], "source": it["source"], "symbols": symbols,
                        "importance": "High" if important else "Medium",
                        "published_at": it["published_at"].isoformat()},
            ))

    def latest(self, symbols: list[str] | None = None, limit: int = 10) -> list[dict]:
        rows = self.items
        if symbols:
            wanted = {s.upper() for s in symbols}
            rows = [r for r in rows if wanted & set(classify_symbols(r["headline"] + " " + r.get("summary", "")))]
        return rows[:limit]

    def snapshot(self) -> dict:
        return {"state": self.state, "detail": self.detail, "source": self.provider.name, "count": len(self.items)}
