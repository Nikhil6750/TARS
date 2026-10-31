"""Replay can never be presented as live (task P2-C3).

The dashboard's honesty rests on one rule: an observation is labelled LIVE
only when a real feed produced it. Until now that held partly by accident --
there was no feed to attach, so nothing could compete with the replay
cursor. P2-C1 attaches one, which is exactly what could erode it: with a
healthy feed connected and the cursor rewound, the panels are still computed
at ``ts <= cursor``, and calling that LIVE would be a lie about data the
user deliberately rewound.

These tests pin the rule down before the feed arrives.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from market import clock, provenance
from market.clock import FeedStatus
from market.provenance import DataSource

# A Wednesday, mid-London session: the market is open, so nothing here is
# CLOSED for an incidental calendar reason.
OPEN_MOMENT = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)
CURSOR = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
STORE_LAST = datetime(2026, 6, 3, 11, 55, tzinfo=UTC)


# ---- the gate, as a pure function --------------------------------------


def test_a_fresh_feed_is_live_when_nothing_is_pinned():
    """The control case: without a replay pin, a healthy feed is LIVE. If
    this ever fails the rest of these tests prove nothing."""
    status = clock.resolve_status(
        now=OPEN_MOMENT,
        feed_last_tick=OPEN_MOMENT - timedelta(seconds=5),
        replay_pinned=False,
        cursor=CURSOR,
        store_last_bar=STORE_LAST,
    )
    assert status.status is FeedStatus.LIVE
    assert status.source == DataSource.LIVE_FEED.value
    assert status.replay is False


def test_a_pinned_replay_cursor_beats_a_perfectly_healthy_feed():
    """The invariant. A tick one second old does not make rewound data
    live."""
    status = clock.resolve_status(
        now=OPEN_MOMENT,
        feed_last_tick=OPEN_MOMENT - timedelta(seconds=1),
        replay_pinned=True,
        cursor=CURSOR,
        store_last_bar=STORE_LAST,
    )
    assert status.status is FeedStatus.REPLAY
    assert status.source == DataSource.RECORDED_STORE.value
    assert status.replay is True
    assert "not a live feed" in status.detail.lower()


@pytest.mark.parametrize("tick_age_seconds", [0, 1, 30, 89, 90, 91, 3600, 86400])
def test_no_feed_age_whatsoever_can_defeat_the_pin(tick_age_seconds):
    """Swept across the freshness boundary: the pin wins at every age, so
    the rule cannot be escaped by timing."""
    status = clock.resolve_status(
        now=OPEN_MOMENT,
        feed_last_tick=OPEN_MOMENT - timedelta(seconds=tick_age_seconds),
        replay_pinned=True,
        cursor=CURSOR,
        store_last_bar=STORE_LAST,
    )
    assert status.status is FeedStatus.REPLAY


def test_resolve_status_is_the_only_path_to_live():
    """`live_status` is the sole producer of LIVE, and the gate is the only
    caller that reaches it. Guards against a future caller going around the
    gate straight to `live_status`."""
    import inspect

    source = inspect.getsource(clock)
    # Count call sites, not the definition.
    calls = source.count("live_status(") - source.count("def live_status(")
    assert calls == 1, (
        "live_status must be called exactly once, from resolve_status; "
        "another call site would be a way around the replay pin"
    )


def test_an_empty_store_is_no_data_not_live():
    status = clock.resolve_status(
        now=OPEN_MOMENT, feed_last_tick=None, replay_pinned=False,
        cursor=None, store_last_bar=None,
    )
    assert status.status is FeedStatus.NO_DATA
    assert status.source == DataSource.RECORDED_STORE.value


def test_no_feed_means_recorded_history():
    status = clock.resolve_status(
        now=OPEN_MOMENT, feed_last_tick=None, replay_pinned=False,
        cursor=CURSOR, store_last_bar=STORE_LAST,
    )
    assert status.status is FeedStatus.REPLAY
    assert status.source == DataSource.RECORDED_STORE.value


def test_a_stale_feed_is_still_not_live_and_says_so():
    status = clock.resolve_status(
        now=OPEN_MOMENT,
        feed_last_tick=OPEN_MOMENT - timedelta(minutes=10),
        replay_pinned=False, cursor=CURSOR, store_last_bar=STORE_LAST,
    )
    assert status.status is FeedStatus.STALE
    # STALE comes from a real feed, so the source is honest about that; the
    # status, not the source, is what says it has aged out.
    assert status.source == DataSource.LIVE_FEED.value


def test_the_status_dict_always_names_its_source():
    for pinned in (True, False):
        payload = clock.resolve_status(
            now=OPEN_MOMENT, feed_last_tick=OPEN_MOMENT, replay_pinned=pinned,
            cursor=CURSOR, store_last_bar=STORE_LAST,
        ).as_dict()
        assert payload["source"] in {s.value for s in DataSource}
        assert "replay" in payload


# ---- provenance --------------------------------------------------------


def test_recorded_provenance_is_never_live():
    p = provenance.recorded(observed_at=CURSOR)
    assert p.source is DataSource.RECORDED_STORE
    assert p.replay is True
    assert p.is_live is False
    assert p.as_dict()["is_live"] is False


def test_live_provenance_names_its_provider():
    p = provenance.live(provider="mt5", observed_at=OPEN_MOMENT)
    assert p.source is DataSource.LIVE_FEED
    assert p.is_live is True
    assert p.as_dict()["provider"] == "mt5"


def test_provenance_round_trips_its_observation_time():
    assert provenance.recorded(observed_at=CURSOR).as_dict()["observed_at"] == CURSOR.isoformat()
    assert provenance.recorded(observed_at=None).as_dict()["observed_at"] is None


# ---- the service boundary ----------------------------------------------
#
# These need the real candle store. Skipped rather than faked when it is
# absent: a fake store would prove the test harness works, not the service.


def _service():
    from market.service import MarketXRayService
    from market.store import CandleStore

    store = CandleStore()
    if not store.available_symbols:
        pytest.skip("candle store is empty; run `python -m market.ingest`")
    svc = MarketXRayService(store=store)
    if svc.bounds[1] is None:
        pytest.skip("candle store has no bars")
    return svc


def test_service_starts_unpinned_and_reports_recorded_data():
    svc = _service()
    assert svc.replay_pinned is False
    status = svc.market_status()
    # No feed is attached, so this must be recorded history either way.
    assert status.status is not FeedStatus.LIVE
    assert svc.data_provenance().is_live is False


def test_moving_the_cursor_pins_replay():
    svc = _service()
    first, last = svc.bounds
    svc.set_cursor(first + (last - first) / 2)
    assert svc.replay_pinned is True


def test_resetting_the_cursor_unpins_replay():
    svc = _service()
    first, last = svc.bounds
    svc.set_cursor(first + (last - first) / 2)
    assert svc.replay_pinned is True
    svc.reset_cursor()
    assert svc.replay_pinned is False


def test_stepping_the_cursor_pins_replay():
    svc = _service()
    svc.reset_cursor()
    svc.step_cursor(timeframe="H1", bars=-24)
    assert svc.replay_pinned is True


def test_a_live_feed_plus_a_replay_cursor_is_never_live():
    """The headline case, through the real service: attach a healthy feed,
    rewind the cursor, and assert nothing reports LIVE."""
    svc = _service()
    first, last = svc.bounds
    svc.set_cursor(first + (last - first) / 2)
    svc.attach_feed(provider="test_feed", last_tick=datetime.now(UTC))

    status = svc.market_status()
    assert status.status is FeedStatus.REPLAY
    assert status.source == DataSource.RECORDED_STORE.value
    assert status.replay is True

    prov = svc.data_provenance()
    assert prov.is_live is False
    assert prov.source is DataSource.RECORDED_STORE
    # The payload must say *why*, not merely that it is not live.
    assert "pinned" in prov.detail.lower()


def test_every_payload_carries_provenance_while_replaying():
    svc = _service()
    first, last = svc.bounds
    svc.set_cursor(first + (last - first) / 2)
    svc.attach_feed(provider="test_feed", last_tick=datetime.now(UTC))
    symbol = svc.store.available_symbols[0]

    payloads = {
        "xray": svc.xray(timeframe="H1"),
        "strength": svc.strength(),
        "leaders": svc.leaders(timeframe="H1"),
        "candles": svc.candles(symbol=symbol, timeframe="H1"),
        "order_blocks": svc.order_blocks(symbol=symbol, timeframe="H1"),
        "events": svc.events(limit=5),
    }
    for name, payload in payloads.items():
        assert "provenance" in payload, f"{name} does not declare its source"
        prov = payload["provenance"]
        assert prov["source"] == DataSource.RECORDED_STORE.value, name
        assert prov["is_live"] is False, f"{name} presents replayed data as live"
        assert prov["replay"] is True, name


def test_the_dashboard_declares_provenance_and_the_pin():
    svc = _service()
    first, last = svc.bounds
    svc.set_cursor(first + (last - first) / 2)
    svc.attach_feed(provider="test_feed", last_tick=datetime.now(UTC))
    dash = svc.dashboard(timeframe="H1")

    assert dash["replay_pinned"] is True
    assert dash["provenance"]["is_live"] is False
    assert dash["status"]["status"] == FeedStatus.REPLAY.value
    assert dash["status"]["source"] == DataSource.RECORDED_STORE.value
    # Every nested panel agrees with the top-level claim.
    for key in ("xray", "strength", "leaders", "chart", "order_blocks", "events"):
        assert dash[key]["provenance"]["is_live"] is False, key


def test_detaching_a_feed_falls_back_to_recorded_history():
    svc = _service()
    svc.reset_cursor()
    svc.attach_feed(provider="test_feed", last_tick=datetime.now(UTC))
    svc.detach_feed()
    assert svc.market_status().status is not FeedStatus.LIVE
    assert svc.data_provenance().source is DataSource.RECORDED_STORE


def test_a_feed_must_name_itself():
    """An unnamed provider would make provenance useless."""
    svc = _service()
    for bad in ("", provenance.NO_PROVIDER):
        with pytest.raises(ValueError):
            svc.attach_feed(provider=bad, last_tick=datetime.now(UTC))


def test_provenance_does_not_persist_a_health_row_per_panel():
    """Stamping provenance on eight panels must not write eight feed-health
    rows -- `_resolve_status` exists precisely so it does not."""
    svc = _service()
    writes = []
    original = svc.state.record_feed_health
    svc.state.record_feed_health = lambda status: writes.append(status)  # type: ignore[method-assign]
    try:
        svc.data_provenance()
        svc.data_provenance()
        assert writes == []
        svc.market_status()
        assert len(writes) == 1
    finally:
        svc.state.record_feed_health = original  # type: ignore[method-assign]
