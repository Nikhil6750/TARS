from __future__ import annotations

from datetime import UTC, datetime

from events.core import EventSource
from monitors.news import NewsMonitor, RssNewsProvider, classify_symbols

_SAMPLE_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
<title>Sample Feed</title>
<item>
  <title>Gold Price Forecast: XAU/USD holds losses near $4,300 amid Fed rate hike bets</title>
  <link>https://example.com/gold-1</link>
  <pubDate>Tue, 22 Sep 2026 05:25:10 GMT</pubDate>
  <description>Gold price inches lower during Asian hours on Tuesday.</description>
</item>
<item>
  <title>EUR/USD steady ahead of ECB speakers</title>
  <link>https://example.com/eurusd-1</link>
  <pubDate>Tue, 22 Sep 2026 04:10:00 GMT</pubDate>
  <description>The pair trades flat.</description>
</item>
<item>
  <title>Unrelated local sports headline</title>
  <link>https://example.com/sports-1</link>
  <pubDate>Tue, 22 Sep 2026 03:00:00 GMT</pubDate>
  <description>A football match happened.</description>
</item>
</channel></rss>"""


def test_classify_symbols_matches_keywords_case_insensitively():
    assert classify_symbols("Gold Price Forecast: XAU/USD holds losses") == ["XAUUSD"]
    assert classify_symbols("EUR/USD steady ahead of ECB speakers") == ["EURUSD"]
    assert classify_symbols("Unrelated local sports headline") == []


def test_rss_provider_parses_well_formed_feed():
    provider = RssNewsProvider("https://example.com/feed", name="test-feed")
    items = provider.parse(_SAMPLE_RSS)
    assert len(items) == 3
    assert items[0]["headline"].startswith("Gold Price Forecast")
    assert items[0]["url"] == "https://example.com/gold-1"
    assert items[0]["published_at"].tzinfo is not None
    assert items[0]["source"] == "test-feed"


def test_rss_provider_returns_empty_on_malformed_xml():
    provider = RssNewsProvider("https://example.com/feed")
    assert provider.parse("not xml at all <<<") == []


class _FakeProvider:
    name = "fake"

    def __init__(self, items: list[dict]):
        self._items = items

    async def latest(self, symbols):
        return self._items


def _item(headline: str, item_id: str, summary: str = "") -> dict:
    return {"id": item_id, "source": "fake", "headline": headline, "published_at": datetime.now(UTC),
            "url": f"https://example.com/{item_id}", "summary": summary}


async def _async_noop(*_args, **_kwargs) -> None:
    return None


async def test_first_tick_never_floods_the_event_core():
    """Existing headlines on startup are not "new" -- publishing all of them at once the first
    time the monitor runs would flood the event core."""
    published = []

    async def publish(event):
        published.append(event)
        return True

    provider = _FakeProvider([_item("Gold surges on Fed surprise", "a"), _item("EUR/USD flat", "b")])
    monitor = NewsMonitor(provider, publish, _async_noop, symbols=["XAUUSD", "EURUSD"], tick_seconds=999)
    await monitor.tick()
    assert published == []
    assert len(monitor.items) == 2


async def test_second_tick_publishes_only_genuinely_new_items():
    published = []

    async def publish(event):
        published.append(event)
        return True

    items = [_item("Gold surges on Fed surprise", "a")]
    provider = _FakeProvider(items)
    monitor = NewsMonitor(provider, publish, _async_noop, symbols=["XAUUSD"], tick_seconds=999)
    await monitor.tick()
    assert published == []

    items.append(_item("Gold plunges after CPI shock", "b"))
    await monitor.tick()
    assert len(published) == 1
    assert published[0].dedupe_key == "b"
    assert published[0].source == EventSource.NEWS


async def test_irrelevant_symbol_items_are_recorded_but_not_published_as_events():
    published = []

    async def publish(event):
        published.append(event)
        return True

    provider = _FakeProvider([])
    monitor = NewsMonitor(provider, publish, _async_noop, symbols=["XAUUSD"], tick_seconds=999)
    await monitor.tick()  # establish first_fetch=False baseline

    provider._items = [_item("Unrelated local sports headline", "c")]
    await monitor.tick()
    assert published == []
    assert len(monitor.items) == 1  # still recorded for latest()/snapshot(), just not alerted


async def test_severity_is_higher_for_high_impact_keywords():
    published = []

    async def publish(event):
        published.append(event)
        return True

    provider = _FakeProvider([])
    monitor = NewsMonitor(provider, publish, _async_noop, symbols=["XAUUSD"], tick_seconds=999)
    await monitor.tick()

    provider._items = [_item("Gold surges after surprise Fed rate hike", "d")]
    await monitor.tick()
    assert published[0].severity == 3

    provider._items.append(_item("Gold ticks up slightly in quiet trade", "e"))
    await monitor.tick()
    quiet = [e for e in published if e.dedupe_key == "e"][0]
    assert quiet.severity == 2


async def test_fetch_failure_reports_error_state_and_does_not_raise():
    async def failing_latest(symbols):
        raise RuntimeError("network down")

    provider = _FakeProvider([])
    provider.latest = failing_latest
    states = []

    async def record_state(state):
        states.append(state)

    monitor = NewsMonitor(provider, _async_noop, record_state, symbols=["XAUUSD"], tick_seconds=999)
    await monitor.tick()
    assert monitor.state == "ERROR"
    assert states == ["ERROR"]


def test_latest_filters_by_symbol():
    monitor = NewsMonitor(_FakeProvider([]), _async_noop, _async_noop, symbols=["XAUUSD"])
    monitor.items = [_item("Gold rallies", "a"), _item("EUR/USD flat", "b")]
    assert [r["id"] for r in monitor.latest(symbols=["XAUUSD"])] == ["a"]
    assert len(monitor.latest()) == 2
