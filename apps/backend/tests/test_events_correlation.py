from __future__ import annotations

from events.correlation import CorrelationEngine
from events.core import EventSource


def _calendar_event_payload(symbol: str = "EURUSD", event_id: str = "cal-1") -> dict:
    return {
        "type": "proactive_event",
        "event": {
            "id": event_id, "source": EventSource.ECONOMIC_CALENDAR.value, "kind": "calendar.upcoming",
            "severity": 3, "symbol": symbol, "title": "USD CPI in 8 min",
            "dedupe_key": "nfp-abc123:calendar.upcoming",  # stable across ticks, unlike `id`
            "payload": {"currency": "USD"},
        },
        "decision": "NOTIFY",
    }


class _FakeCore:
    def __init__(self):
        self.published = []

    async def publish(self, event) -> bool:
        self.published.append(event)
        return True


class _FakeHotChartStore:
    def __init__(self, symbol: str | None, freshness: str = "hot"):
        self._symbol, self._freshness = symbol, freshness

    async def get_latest(self):
        if self._symbol is None:
            return None
        from types import SimpleNamespace

        return SimpleNamespace(
            identity=SimpleNamespace(symbol=self._symbol),
            freshness=lambda: SimpleNamespace(value=self._freshness),
        )


class _FakeMonitors:
    def __init__(self, positions: list[dict], chart_symbol: str | None, freshness: str = "hot"):
        self.mt5 = _FakeMt5(positions)
        self.hot_chart_store = _FakeHotChartStore(chart_symbol, freshness)


class _FakeMt5:
    def __init__(self, positions: list[dict]):
        self._positions = positions

    def snapshot(self) -> dict:
        return {"positions": self._positions}


async def test_publishes_correlated_alert_when_all_three_conditions_align():
    core = _FakeCore()
    monitors = _FakeMonitors(positions=[{"symbol": "EURUSD", "ticket": 1}], chart_symbol="EURUSD")
    engine = CorrelationEngine(core, monitors)

    await engine._on_event(_calendar_event_payload("EURUSD"))

    assert len(core.published) == 1
    event = core.published[0]
    assert event.source == EventSource.SYSTEM
    assert event.kind == "correlated_alert"
    assert event.severity == 4
    assert event.symbol == "EURUSD"
    assert "position" in event.summary.lower() and "tradingview" in event.summary.lower()


async def test_correlated_dedupe_key_is_stable_across_repeated_ticks_of_the_same_calendar_entry():
    """RealtimeEventCore cools down on dedupe_key, not on the event's own random `id` (a fresh
    UUID every publish) -- the correlated alert must key off the same stable dedupe_key the
    triggering calendar event carries, or it would re-fire as a "new" alert on every tick within
    the whole 15-minute lead window instead of cooling down like any other source's alert."""
    core = _FakeCore()
    monitors = _FakeMonitors(positions=[{"symbol": "EURUSD"}], chart_symbol="EURUSD")
    engine = CorrelationEngine(core, monitors)

    await engine._on_event(_calendar_event_payload("EURUSD", event_id="random-uuid-1"))
    await engine._on_event(_calendar_event_payload("EURUSD", event_id="random-uuid-2"))

    assert len(core.published) == 2
    assert core.published[0].dedupe_key == core.published[1].dedupe_key
    assert core.published[0].dedupe_key == "correlated:nfp-abc123:calendar.upcoming"


async def test_no_alert_when_no_open_position():
    core = _FakeCore()
    monitors = _FakeMonitors(positions=[], chart_symbol="EURUSD")
    engine = CorrelationEngine(core, monitors)
    await engine._on_event(_calendar_event_payload("EURUSD"))
    assert core.published == []


async def test_no_alert_when_not_watching_on_tradingview():
    core = _FakeCore()
    monitors = _FakeMonitors(positions=[{"symbol": "EURUSD"}], chart_symbol=None)
    engine = CorrelationEngine(core, monitors)
    await engine._on_event(_calendar_event_payload("EURUSD"))
    assert core.published == []


async def test_no_alert_when_watching_a_different_symbol():
    core = _FakeCore()
    monitors = _FakeMonitors(positions=[{"symbol": "EURUSD"}], chart_symbol="XAUUSD")
    engine = CorrelationEngine(core, monitors)
    await engine._on_event(_calendar_event_payload("EURUSD"))
    assert core.published == []


async def test_no_alert_when_chart_state_is_stale():
    core = _FakeCore()
    monitors = _FakeMonitors(positions=[{"symbol": "EURUSD"}], chart_symbol="EURUSD", freshness="stale")
    engine = CorrelationEngine(core, monitors)
    await engine._on_event(_calendar_event_payload("EURUSD"))
    assert core.published == []


async def test_ignores_non_calendar_events():
    core = _FakeCore()
    monitors = _FakeMonitors(positions=[{"symbol": "EURUSD"}], chart_symbol="EURUSD")
    engine = CorrelationEngine(core, monitors)
    payload = {"type": "proactive_event", "event": {
        "id": "mt5-1", "source": EventSource.MT5.value, "kind": "price.move", "severity": 3,
        "symbol": "EURUSD", "title": "EURUSD moved", "payload": {},
    }, "decision": "NOTIFY"}
    await engine._on_event(payload)
    assert core.published == []


async def test_ignores_non_proactive_event_payloads():
    core = _FakeCore()
    monitors = _FakeMonitors(positions=[{"symbol": "EURUSD"}], chart_symbol="EURUSD")
    engine = CorrelationEngine(core, monitors)
    await engine._on_event({"type": "provider_status", "source": "MT5", "status": "CONNECTED"})
    assert core.published == []


def test_start_subscribes_and_stop_unsubscribes():
    calls = {"subscribed": None, "unsubscribed": False}

    class _Core:
        def subscribe(self, callback):
            calls["subscribed"] = callback

            def unsubscribe():
                calls["unsubscribed"] = True

            return unsubscribe

    engine = CorrelationEngine(_Core(), _FakeMonitors([], None))
    engine.start()
    assert calls["subscribed"] == engine._on_event
    engine.stop()
    assert calls["unsubscribed"] is True
