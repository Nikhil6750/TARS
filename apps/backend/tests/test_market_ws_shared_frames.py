"""Sharing frame builds between WebSocket connections.

A reconnect storm used to leave a build per abandoned socket between a new
client and its first frame. Builds are now shared, which is only acceptable if
sharing cannot change what a client is told. So besides the sharing itself,
these tests pin the ways it could go wrong: a frame filed under a moment it was
not computed at, panels outliving the data they came from, and above all a LIVE
that outlives its feed.

The service tests use the real service, store and frame build over a small
generated store, and only the clock is simulated.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import socket
import threading
import time
from concurrent.futures import CancelledError
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import duckdb
import pytest
import uvicorn
import websockets
from fastapi import FastAPI
from starlette.websockets import WebSocketDisconnect

from app.routers import market_xray
from market import clock, universe
from market import service as service_module
from market import state as market_state
from market.clock import FeedStatus
from market.frames import SharedFrames
from market.service import MarketXRayService
from market.store import Candle, CandleStore

# Wednesday mid-session, so nothing is CLOSED by accident of the calendar.
NOW = datetime(2026, 6, 3, 12, 2, 30, tzinfo=UTC)
RECORDED_END = datetime(2026, 5, 29, 21, 0, tzinfo=UTC)
BARS = 300


# ---- the build sharing, alone -------------------------------------------


class Gate:
    """A build that runs until it is told to finish."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.runs = 0

    def __call__(self):
        self.runs += 1
        self.entered.set()
        assert self.release.wait(5)
        return {"type": "market.frame", "run": self.runs}


def value(flight):
    return flight.work.result(timeout=5)


@pytest.fixture
def shared():
    return SharedFrames(retained=2)


def test_everyone_waiting_for_the_same_key_shares_one_build(shared):
    gate = Gate()
    flights = [shared.join("k", gate) for _ in range(120)]
    gate.entered.wait(5)
    gate.release.set()
    assert {value(f)["run"] for f in flights} == {1}
    assert gate.runs == 1


def test_a_finished_frame_is_reused_without_building(shared):
    gate = Gate()
    gate.release.set()
    first = value(shared.join("k", gate))
    assert value(shared.join("k", gate)) is first
    assert gate.runs == 1


def test_different_keys_are_never_shared(shared):
    gate_a, gate_b = Gate(), Gate()
    gate_a.release.set()
    gate_b.release.set()
    assert value(shared.join("a", gate_a)) is not value(shared.join("b", gate_b))
    assert gate_a.runs == gate_b.runs == 1


def test_a_build_nobody_wants_never_starts(shared):
    running, queued = Gate(), Gate()
    shared.join("running", running)
    running.entered.wait(5)
    flight = shared.join("queued", queued)
    shared.leave(flight)
    running.release.set()
    with pytest.raises(CancelledError):
        flight.work.result(timeout=5)
    assert queued.runs == 0
    assert shared.counts()["flights"] <= 1


def test_a_queued_build_survives_while_anyone_still_wants_it(shared):
    running, queued = Gate(), Gate()
    queued.release.set()
    shared.join("running", running)
    running.entered.wait(5)
    leaving, staying = shared.join("queued", queued), shared.join("queued", queued)
    shared.leave(leaving)
    running.release.set()
    assert value(staying)["run"] == 1


def test_a_running_build_outlives_its_clients_and_is_reused(shared):
    gate = Gate()
    abandoned = shared.join("k", gate)
    gate.entered.wait(5)
    shared.leave(abandoned)
    arriving = shared.join("k", gate)
    assert arriving is abandoned, "joined the build already under way"
    gate.release.set()
    assert value(arriving)["run"] == 1
    assert gate.runs == 1


def test_builds_never_overlap(shared):
    active, peak = [0], [0]
    lock = threading.Lock()

    def build():
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.01)
        with lock:
            active[0] -= 1
        return {}

    for flight in [shared.join(key, build) for key in range(10)]:
        value(flight)
    assert peak[0] == 1


def test_a_failure_reaches_every_waiter_and_is_not_kept(shared):
    calls = []
    entered, release = threading.Event(), threading.Event()

    def boom():
        calls.append(1)
        entered.set()
        release.wait(5)
        raise ValueError("bad store")

    flights = [shared.join("k", boom), shared.join("k", boom)]
    entered.wait(5)
    release.set()
    for flight in flights:
        with pytest.raises(ValueError):
            value(flight)
    assert len(calls) == 1

    gate = Gate()
    gate.release.set()
    assert value(shared.join("k", gate))["run"] == 1, "the next request tried again"


def test_kept_frames_are_bounded(shared):
    for key in range(50):
        value(shared.join(key, dict))
    assert shared.counts() == {"flights": 0, "retained": 2}


def test_abandoned_work_does_not_accumulate(shared):
    """The storm itself: a hundred distinct builds, every client gone."""
    running = Gate()
    shared.join("running", running)
    running.entered.wait(5)
    built = []
    for key in range(100):
        shared.leave(shared.join(key, lambda k=key: built.append(k) or {}))
    assert shared.counts()["flights"] == 1
    running.release.set()
    value(shared.join("after", dict))
    assert built == []


# ---- what panels are shared under ---------------------------------------


def _write_recorded_store(root) -> None:
    for symbol in universe.SYMBOLS:
        directory = root / symbol
        directory.mkdir(parents=True)
        for timeframe, minutes in universe.TIMEFRAMES.items():
            start = RECORDED_END - timedelta(minutes=minutes * BARS)
            rows = ", ".join(
                f"(TIMESTAMP '{(start + timedelta(minutes=minutes * i)).strftime('%Y-%m-%d %H:%M:%S')}',"
                f" 1.0, 1.01, 0.99, {1.0 + ((i * i) % 7) * 0.001}, 0.0001, 1.0)"
                for i in range(BARS)
            )
            target = str(directory / f"{timeframe}.parquet").replace("'", "''")
            duckdb.sql(
                f"COPY (SELECT * FROM (VALUES {rows}) t(ts, open, high, low, close, spread, volume)) "
                f"TO '{target}' (FORMAT PARQUET)"
            )


@pytest.fixture(scope="module")
def store_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("recorded")
    _write_recorded_store(root)
    return root


@pytest.fixture
def service(tmp_path, store_root):
    return MarketXRayService(
        store=CandleStore(store_root), state=market_state.MarketState(tmp_path / "state.db")
    )


class Clock:
    """Stands in for the service's wall clock."""

    def __init__(self, monkeypatch, now):
        self.now = now
        moment = self

        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment.now

        monkeypatch.setattr(service_module, "datetime", Frozen)


@pytest.fixture
def wall_clock(monkeypatch):
    return Clock(monkeypatch, NOW)


def follow_feed(service: MarketXRayService, edge: datetime | None = None) -> None:
    """A real feed's reports, with bars up to `edge` for every instrument."""
    edge = edge or datetime(2026, 6, 3, 12, 0, tzinfo=UTC)
    service.attach_feed(provider="mt5:test", last_tick=NOW - timedelta(seconds=5))
    with service._lock:
        service._feed_connection = "CONNECTED"
    for symbol in universe.SYMBOLS:
        for timeframe, minutes in universe.TIMEFRAMES.items():
            span = timedelta(minutes=minutes)
            service.store.put_live_bars(symbol, timeframe, [
                Candle(edge - span * i, 2.0, 2.01, 1.99, 2.0 + i * 0.0001, 0.0001, 1.0)
                for i in range(1, 150)
            ])
    service.report_live_edge(edge)


def built(inputs) -> dict:
    return market_xray._build_frame(inputs, inputs.timeframe)


def sent(service: MarketXRayService, inputs, frame: dict) -> dict:
    return market_xray._labelled(frame, service.frame_labels(inputs))


def assert_not_live(frame: dict) -> None:
    assert frame["status"]["status"] != FeedStatus.LIVE.value
    assert frame["provenance"]["is_live"] is False
    for panel in ("xray", "strength", "leaders"):
        assert frame[panel]["provenance"]["is_live"] is False, panel


def test_equal_inputs_are_equal(service):
    assert service.frame_inputs("H1") == service.frame_inputs("H1")
    assert hash(service.frame_inputs("H1")) == hash(service.frame_inputs("H1"))


def test_a_timeframe_is_its_own_key(service):
    assert service.frame_inputs("H1") != service.frame_inputs("M5")


def test_a_replay_cursor_is_its_own_key(service):
    before = service.frame_inputs("H1")
    service.step_cursor(timeframe="H1", bars=-3)
    assert service.frame_inputs("H1") != before


def test_a_new_live_bar_is_a_new_key_even_at_the_same_cursor(service):
    """The cursor is not enough: the feed writes bars before moving the edge,
    and rewrites ones already at or behind it."""
    follow_feed(service)
    before = service.frame_inputs("H1")
    edge = before.as_of
    service.store.put_live_bars("EURUSD", "H1", [
        Candle(edge - timedelta(hours=2), 3.0, 3.1, 2.9, 3.0, 0.0001, 1.0)
    ])
    assert service.frame_inputs("H1").as_of == edge
    assert service.frame_inputs("H1") != before


def test_a_feed_taking_over_is_a_new_key(service):
    before = service.frame_inputs("H1")
    follow_feed(service, service.cursor)
    assert service.frame_inputs("H1").as_of == before.as_of
    assert service.frame_inputs("H1") != before


def test_a_replaced_service_shares_nothing_with_the_old_one(service, tmp_path, store_root):
    """An import replaces the service, and the same moment means new data."""
    replacement = MarketXRayService(
        store=CandleStore(store_root), state=market_state.MarketState(tmp_path / "other.db")
    )
    assert service.frame_inputs("H1") != replacement.frame_inputs("H1")


# ---- panels stay at the moment they were asked for ----------------------


def test_a_cursor_moving_mid_build_cannot_change_the_panels(service):
    start = service.frame_inputs("H1")
    expected = built(start)
    real_status = service.market_status

    def status_as_the_cursor_moves():
        service.step_cursor(timeframe="H1", bars=-50)
        return real_status()

    # The first thing a build does, so every panel runs after the move.
    service.market_status = status_as_the_cursor_moves
    frame = built(start)
    assert service.cursor != start.as_of
    assert frame["cursor"] == start.as_of.isoformat()
    for panel in ("xray", "strength", "leaders"):
        assert frame[panel]["as_of"] == start.as_of.isoformat(), panel
    assert frame["xray"]["series"] == expected["xray"]["series"]
    assert frame["strength"]["ranking"] == expected["strength"]["ranking"]
    assert frame["leaders"]["bullish"] == expected["leaders"]["bullish"]


def test_a_shared_frame_shows_nothing_after_its_cursor(service):
    """Look-ahead, through the whole path: step back, build, and compare with
    what the store holds at that moment."""
    service.step_cursor(timeframe="M5", bars=-120)
    inputs = service.frame_inputs("M5")
    frame = sent(service, inputs, built(inputs))
    newest_allowed = inputs.as_of - timedelta(minutes=5)
    assert frame["xray"]["series"], "nothing was computed"
    for series in frame["xray"]["series"]:
        assert all(
            datetime.fromisoformat(p["ts"]) <= newest_allowed for p in series["points"]
        ), series["symbol"]
    close = service.store.latest_close("EURUSD", "M5", as_of=inputs.as_of).close
    eurusd = next(s for s in frame["xray"]["series"] if s["symbol"] == "EURUSD")
    assert eurusd["current_close"] == close


def test_stepping_back_to_a_kept_moment_gets_that_moments_panels(service):
    shared = SharedFrames(retained=4)

    def frame_now():
        inputs = service.frame_inputs("H1")
        return value(shared.join(inputs, lambda: built(inputs)))

    service.step_cursor(timeframe="H1", bars=-20)
    earlier = frame_now()
    service.step_cursor(timeframe="H1", bars=10)
    later = frame_now()
    service.step_cursor(timeframe="H1", bars=-10)
    again = frame_now()
    assert again is earlier
    assert again["cursor"] != later["cursor"]
    assert later["xray"]["series"] != earlier["xray"]["series"]


def test_panels_are_what_the_service_itself_computes(service):
    """Sharing is a saving, not a different answer."""
    inputs = service.frame_inputs("H1")
    frame = built(inputs)
    assert frame["xray"]["series"] == service.xray(timeframe="H1")["series"]
    assert frame["strength"]["ranking"] == service.strength()["ranking"]
    assert frame["leaders"]["bullish"] == service.leaders(timeframe="H1")["bullish"]


# ---- labels belong to the moment of sending -----------------------------


def test_the_control_a_healthy_feed_is_live(service, wall_clock):
    """If this fails, the tests below prove nothing."""
    follow_feed(service)
    inputs = service.frame_inputs("H1")
    frame = sent(service, inputs, built(inputs))
    assert frame["status"]["status"] == FeedStatus.LIVE.value
    for labelled in (frame, *(frame[p] for p in ("xray", "strength", "leaders"))):
        assert labelled["provenance"]["is_live"] is True


def test_kept_panels_stop_being_live_when_the_feed_ages_out(service, wall_clock):
    """The rule behind `frame_labels`. No bar changes, so the key does not, so
    this is the same kept frame; only the clock moved."""
    follow_feed(service)
    inputs = service.frame_inputs("H1")
    frame = built(inputs)
    assert sent(service, inputs, frame)["status"]["status"] == FeedStatus.LIVE.value

    wall_clock.now = NOW + timedelta(seconds=clock.LIVE_FRESHNESS_SECONDS + 1)
    assert service.frame_inputs("H1") == inputs, "the key must not be what saves us"
    stale = sent(service, inputs, frame)
    assert stale["status"]["status"] == FeedStatus.STALE.value
    assert_not_live(stale)


def test_kept_panels_stop_being_live_when_the_market_closes(service, wall_clock):
    follow_feed(service)
    inputs = service.frame_inputs("H1")
    frame = built(inputs)
    wall_clock.now = datetime(2026, 6, 6, 12, 0, tzinfo=UTC)  # a Saturday
    closed = sent(service, inputs, frame)
    assert closed["status"]["status"] == FeedStatus.CLOSED.value
    assert_not_live(closed)


def test_kept_panels_stop_being_live_when_the_connection_drops(service, wall_clock):
    follow_feed(service)
    inputs = service.frame_inputs("H1")
    frame = built(inputs)
    with service._lock:
        service._feed_connection = "DISCONNECTED"
    dropped = sent(service, inputs, frame)
    assert dropped["status"]["status"] == FeedStatus.STALE.value
    assert_not_live(dropped)


def test_panels_built_while_live_are_not_live_once_replay_is_pinned(service, wall_clock):
    """A build under way when the user rewinds, landing after they did."""
    follow_feed(service)
    inputs = service.frame_inputs("H1")
    frame = built(inputs)
    service.step_cursor(timeframe="H1", bars=-5)
    pinned = sent(service, inputs, frame)
    assert_not_live(pinned)
    assert pinned["replay_pinned"] is True
    assert pinned["cursor"] == inputs.as_of.isoformat()


def test_panels_built_at_a_pin_are_not_live_once_it_is_lifted(service, wall_clock):
    """The other way round, and the dangerous one: history, in flight while
    the user returns to the live edge, must not land labelled LIVE."""
    follow_feed(service)
    # Far enough back to land inside recorded history, not on its last bar,
    # which is where the reset below returns the replay cursor.
    service.step_cursor(timeframe="H1", bars=-200)
    inputs = service.frame_inputs("H1")
    assert inputs.as_of < service.bounds[1]
    frame = built(inputs)
    service.reset_cursor()
    assert service.market_status().status is FeedStatus.LIVE, "the control: the service is live"
    returned = sent(service, inputs, frame)
    assert_not_live(returned)
    assert returned["status"]["replay"] is True
    assert returned["status"]["data_as_of"] == inputs.as_of.isoformat()
    assert returned["provenance"]["observed_at"] == inputs.as_of.isoformat()
    assert returned["replay_pinned"] is True


def test_panels_from_before_a_feed_attached_are_not_live(service, wall_clock):
    inputs = service.frame_inputs("H1")
    frame = built(inputs)
    follow_feed(service)
    assert_not_live(sent(service, inputs, frame))


def test_recorded_history_is_labelled_as_such(service):
    inputs = service.frame_inputs("H1")
    frame = sent(service, inputs, built(inputs))
    assert frame["status"]["status"] == FeedStatus.REPLAY.value
    assert frame["status"]["source"] == "RECORDED_STORE"
    assert frame["provenance"]["source"] == "RECORDED_STORE"
    assert_not_live(frame)


def test_every_provenance_in_a_frame_agrees(service, wall_clock):
    follow_feed(service)
    inputs = service.frame_inputs("H1")
    frame = sent(service, inputs, built(inputs))
    for panel in ("xray", "strength", "leaders"):
        assert frame[panel]["provenance"] == frame["provenance"], panel


def test_labelling_never_touches_the_shared_frame(service, wall_clock):
    follow_feed(service)
    inputs = service.frame_inputs("H1")
    frame = built(inputs)
    before = copy.deepcopy(frame)
    wall_clock.now = NOW + timedelta(seconds=120)
    sent(service, inputs, frame)
    assert frame == before


def test_a_shared_frame_carries_no_labels_of_its_own(service):
    frame = built(service.frame_inputs("H1"))
    assert not {"sent_at", "status", "provenance", "replay_pinned", "feed"} & set(frame)


def test_labels_do_no_io(service):
    """They run on the event loop for every frame sent."""
    service.state.record_feed_health = None
    inputs = service.frame_inputs("H1")
    service.frame_labels(inputs)


def test_the_status_frame_keeps_its_shape(tmp_path):
    """An empty store has no panels and no cursor, and still gets a frame."""
    empty = MarketXRayService(
        store=CandleStore(tmp_path / "empty"), state=market_state.MarketState(tmp_path / "s.db")
    )
    inputs = empty.frame_inputs("H1")
    assert inputs.as_of is None
    frame = sent(empty, inputs, built(inputs))
    assert set(frame) == {"type", "status", "feed"}
    assert frame["type"] == "market.status"
    assert frame["status"]["status"] == FeedStatus.NO_DATA.value


# ---- feed health no longer depends on someone being connected -----------


def test_feed_health_is_recorded_by_the_feed_not_by_frames(service, wall_clock):
    """Per-frame writes went with per-frame builds, and a feed that drops
    changes nothing a build depends on, so the record has to come from here:
    with no client connected and no frame built, it still has to say so."""
    from market import feed

    follow_feed(service)
    assert service.market_status().status is FeedStatus.LIVE
    state = feed.FeedState(
        connection=feed.FeedConnection.DISCONNECTED, detail="lost", provider=None,
        instruments_expected=18, instruments_ticking=0, instruments_missing=[],
        server_offset_seconds=None,
    )
    service.report_feed(state, last_tick=None, connected=False)
    assert service.state.feed_health()["status"] == FeedStatus.STALE.value


# ---- a fresh backend ----------------------------------------------------


def test_clients_arriving_together_build_one_service(monkeypatch, tmp_path):
    class Stub:
        count = 0

        def __init__(self):
            time.sleep(0.05)
            Stub.count += 1
            self.store = SimpleNamespace(recorded_revision="legacy", root=tmp_path)

    monkeypatch.setattr(market_xray, "MarketXRayService", Stub)
    monkeypatch.setattr(market_xray, "_start_feed", lambda _app: None)

    class State:
        market_xray_service = None

    class App:
        state = State()

    served = []
    failures = []

    def arrive():
        try:
            served.append(market_xray._app_service(App))
        except Exception as exc:  # noqa: BLE001 - a thread's failure ends the thread silently
            failures.append(exc)

    threads = [threading.Thread(target=arrive) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    assert Stub.count == 1
    assert len(served) == 20 and len({id(service) for service in served}) == 1


def test_a_failed_service_is_retried_by_the_next_client(monkeypatch):
    def unavailable():
        raise RuntimeError("no store")

    monkeypatch.setattr(market_xray, "MarketXRayService", unavailable)

    class State:
        market_xray_service = None

    class App:
        state = State()

    for _ in range(2):
        with pytest.raises(market_xray.HTTPException):
            market_xray._app_service(App)


# ---- through the real handler -------------------------------------------


class Client:
    app = object()

    def __init__(self, script=()):
        self.script = list(script)
        self.sent: list[dict] = []

    async def accept(self):
        pass

    async def receive_json(self):
        while not self.script:
            await asyncio.sleep(0.002)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def send_json(self, frame):
        self.sent.append(frame)

    async def close(self, code=1000, reason=None):
        pass


@pytest.fixture
def counted(monkeypatch, service):
    """The real service and build, counted."""
    monkeypatch.setattr(market_xray, "_app_service", lambda _app: service)
    real = market_xray._build_frame
    log = {"builds": []}

    def build(inputs, timeframe):
        log["builds"].append(timeframe)
        return real(inputs, timeframe)

    monkeypatch.setattr(market_xray, "_build_frame", build)
    return log


async def _first_frame(client: Client) -> dict:
    deadline = time.time() + 15
    while not client.sent and time.time() < deadline:
        await asyncio.sleep(0.005)
    assert client.sent, "no frame arrived"
    return client.sent[0]


async def _finish(*tasks) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_many_connections_at_once_build_once(counted):
    clients = [Client() for _ in range(30)]
    tasks = [asyncio.create_task(market_xray.market_ws(c)) for c in clients]
    frames = [await _first_frame(c) for c in clients]
    await _finish(*tasks)
    assert len(counted["builds"]) == 1
    assert {f["cursor"] for f in frames} == {frames[0]["cursor"]}
    assert all(f["type"] == "market.frame" for f in frames)


async def test_each_subscription_gets_its_own_timeframe(counted, monkeypatch):
    monkeypatch.setattr(market_xray, "PUSH_INTERVAL_SECONDS", 0.01)
    plain, subscribed = Client(), Client([{"type": "subscribe", "timeframe": "H4"}])
    tasks = [asyncio.create_task(market_xray.market_ws(c)) for c in (plain, subscribed)]
    deadline = time.time() + 15
    while time.time() < deadline and not (
        plain.sent and any(f["timeframe"] == "H4" for f in subscribed.sent)
    ):
        await asyncio.sleep(0.01)
    await _finish(*tasks)
    assert {f["timeframe"] for f in plain.sent} == {universe.DEFAULT_TIMEFRAME}
    h4 = next(f for f in subscribed.sent if f["timeframe"] == "H4")
    assert {s["timeframe"] for s in h4["xray"]["series"]} == {"H4"}
    assert h4["leaders"]["timeframe"] == "H4"


async def test_a_replay_step_reaches_a_connection_sharing_frames(counted, service, monkeypatch):
    monkeypatch.setattr(market_xray, "PUSH_INTERVAL_SECONDS", 0.01)
    client = Client()
    task = asyncio.create_task(market_xray.market_ws(client))
    first = await _first_frame(client)
    client.script.append({"type": "replay.step", "bars": -4})
    deadline = time.time() + 15
    while time.time() < deadline and client.sent[-1]["cursor"] == first["cursor"]:
        await asyncio.sleep(0.01)
    await _finish(task)
    assert client.sent[-1]["cursor"] == service.cursor.isoformat() != first["cursor"]
    assert client.sent[-1]["replay_pinned"] is True


async def test_repeat_pushes_reuse_panels_and_refresh_labels(counted, service, monkeypatch):
    monkeypatch.setattr(market_xray, "PUSH_INTERVAL_SECONDS", 0.01)
    client = Client()
    task = asyncio.create_task(market_xray.market_ws(client))
    deadline = time.time() + 15
    while time.time() < deadline and len(client.sent) < 5:
        await asyncio.sleep(0.01)
    await _finish(task)
    assert len(counted["builds"]) == 1
    assert len({f["sent_at"] for f in client.sent}) > 1, "labels were frozen with the panels"


async def test_a_build_failure_is_shared_as_an_error_frame_and_retried(service, monkeypatch):
    monkeypatch.setattr(market_xray, "_app_service", lambda _app: service)
    monkeypatch.setattr(market_xray, "PUSH_INTERVAL_SECONDS", 0.01)
    calls = []

    def boom(_inputs, _timeframe):
        calls.append(1)
        raise ValueError("bad store")

    monkeypatch.setattr(market_xray, "_build_frame", boom)
    client = Client()
    task = asyncio.create_task(market_xray.market_ws(client))
    deadline = time.time() + 15
    while time.time() < deadline and len(client.sent) < 3:
        await asyncio.sleep(0.005)
    await _finish(task)
    assert {f["type"] for f in client.sent} == {"market.error"}
    assert len(calls) >= 3


async def test_a_replaced_service_is_picked_up_mid_connection(
    service, tmp_path, store_root, monkeypatch
):
    current = {"service": service}
    monkeypatch.setattr(market_xray, "_app_service", lambda _app: current["service"])
    monkeypatch.setattr(market_xray, "PUSH_INTERVAL_SECONDS", 0.01)
    real = market_xray._build_frame
    builders = []
    monkeypatch.setattr(
        market_xray, "_build_frame",
        lambda inputs, tf: (builders.append(inputs.service), real(inputs, tf))[1],
    )
    client = Client()
    task = asyncio.create_task(market_xray.market_ws(client))
    await _first_frame(client)
    replacement = MarketXRayService(
        store=CandleStore(store_root), state=market_state.MarketState(tmp_path / "next.db")
    )
    current["service"] = replacement
    deadline = time.time() + 15
    while time.time() < deadline and replacement not in builders:
        await asyncio.sleep(0.01)
    await _finish(task)
    assert replacement in builders


# ---- a real server, really aborted --------------------------------------


class ErrorCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())


@pytest.fixture
def server(service, monkeypatch):
    monkeypatch.setattr(market_xray, "_app_service", lambda _app: service)
    monkeypatch.setattr(market_xray, "_frames", SharedFrames(retained=8))
    active = {"now": 0, "peak": 0, "total": 0}
    lock = threading.Lock()
    real = market_xray._build_frame

    def build(inputs, timeframe):
        with lock:
            active["now"] += 1
            active["total"] += 1
            active["peak"] = max(active["peak"], active["now"])
        try:
            return real(inputs, timeframe)
        finally:
            with lock:
                active["now"] -= 1

    monkeypatch.setattr(market_xray, "_build_frame", build)
    app = FastAPI()
    app.include_router(market_xray.router)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    uv = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, ws="websockets")
    )
    errors = ErrorCapture()
    logging.getLogger("uvicorn.error").addHandler(errors)
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not uv.started and time.time() < deadline:
        time.sleep(0.02)
    assert uv.started

    yield f"ws://127.0.0.1:{port}/api/v1/market/ws", uv, errors, active

    uv.should_exit = True
    thread.join(timeout=15)
    logging.getLogger("uvicorn.error").removeHandler(errors)


async def _abort_after(url: str, delay: float, subscribe: str | None = None) -> None:
    connection = await websockets.connect(url, max_size=None)
    if subscribe:
        await connection.send(json.dumps({"type": "subscribe", "timeframe": subscribe}))
    await asyncio.sleep(delay)
    connection.transport.abort()


async def _settle(uv, active) -> None:
    deadline = time.time() + 15
    while time.time() < deadline:
        if not uv.server_state.connections and not uv.server_state.tasks and not active["now"]:
            return
        await asyncio.sleep(0.02)


async def test_a_fresh_client_after_a_storm_waits_for_nothing_abandoned(server):
    url, uv, errors, active = server
    # Past the first subscription wait, or the storm never starts a build.
    delay = market_xray.INITIAL_SUBSCRIPTION_SECONDS + 0.02
    await asyncio.gather(*[_abort_after(url, delay) for _ in range(120)])

    async with websockets.connect(url, max_size=None) as connection:
        frame = json.loads(await asyncio.wait_for(connection.recv(), 5))
    assert frame["type"] == "market.frame"
    assert frame["status"]["status"] == FeedStatus.REPLAY.value
    assert frame["provenance"]["is_live"] is False

    await _settle(uv, active)
    assert errors.records == [], errors.records[:3]
    assert not uv.server_state.connections and not uv.server_state.tasks
    assert active["total"] == 1, "120 identical clients needed one build"
    assert active["peak"] == 1


async def test_a_storm_across_every_timeframe_stays_bounded(server):
    url, uv, errors, active = server
    timeframes = list(universe.TIMEFRAMES)
    await asyncio.gather(*[
        _abort_after(url, 0.02, timeframes[i % len(timeframes)]) for i in range(100)
    ])
    await _settle(uv, active)
    assert errors.records == [], errors.records[:3]
    assert active["peak"] == 1
    assert active["total"] <= len(timeframes)
    assert market_xray._frames.counts()["flights"] == 0


async def test_repeated_storms_leave_nothing_behind(server):
    url, uv, errors, active = server
    wait = market_xray.INITIAL_SUBSCRIPTION_SECONDS
    for _ in range(4):
        await asyncio.gather(*[_abort_after(url, wait + 0.002 * i) for i in range(40)])
    await _settle(uv, active)
    assert active["total"] > 0, "nothing was exercised"
    counts = market_xray._frames.counts()
    assert counts["flights"] == 0
    assert counts["retained"] <= 8
    assert errors.records == [], errors.records[:3]
    assert not uv.server_state.connections and not uv.server_state.tasks
    assert active["now"] == 0


async def test_a_disconnect_during_a_shared_build_never_sends(service, monkeypatch):
    """The send-after-close rule holds when the build is not this client's."""
    monkeypatch.setattr(market_xray, "_app_service", lambda _app: service)
    release = threading.Event()
    real = market_xray._build_frame

    def slow(inputs, timeframe):
        release.wait(5)
        return real(inputs, timeframe)

    monkeypatch.setattr(market_xray, "_build_frame", slow)
    staying, leaving = Client(), Client([WebSocketDisconnect(code=1000)])
    stayed = asyncio.create_task(market_xray.market_ws(staying))
    left = asyncio.create_task(market_xray.market_ws(leaving))
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.wait_for(left, 10)
    await _first_frame(staying)
    await _finish(stayed)
    assert leaving.sent == []
    assert staying.sent[0]["type"] == "market.frame"
