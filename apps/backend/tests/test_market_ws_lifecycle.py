"""The Market WebSocket's connection lifecycle.

A frame takes seconds to build, in a thread, and a client can vanish at any point
during that. The handler must then stop quietly: no write to a closed socket, no
second close, and no build left running behind a finished handler. It must also
stop being quiet about anything that is not a disconnect.

The stress tests at the end use a real uvicorn server and really abort the TCP
connection, because the failure lives in the seam between the handler and the
server, which a fake socket can only imitate.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time

import pytest
import uvicorn
import websockets
from fastapi import FastAPI
from starlette.websockets import WebSocketDisconnect

from app.routers import market_xray

UVICORN_CLOSED_ERROR = (
    "Unexpected ASGI message 'websocket.send', after sending 'websocket.close' "
    "or response already completed."
)


class FakeSocket:
    """A connection scripted from a list of client messages. Each entry is a
    message, an exception to raise, or nothing at all, which waits forever."""

    app = object()

    def __init__(self, script=()):
        self.script = list(script)
        self.sent: list[dict] = []
        self.closed_with: list[int] = []
        self.send_error: Exception | None = None
        self.peer_gone = False

    async def accept(self):
        pass

    async def receive_json(self):
        while not self.script:
            await asyncio.sleep(0.002)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            if isinstance(item, WebSocketDisconnect):
                self.peer_gone = True
            raise item
        return item

    async def send_json(self, frame):
        if self.send_error is not None:
            raise self.send_error
        if self.peer_gone:
            raise RuntimeError(UVICORN_CLOSED_ERROR)
        self.sent.append(frame)

    async def close(self, code=1000, reason=None):
        self.closed_with.append(code)


class StubService:
    bounds = (object(), object())

    def __init__(self):
        self.steps: list[tuple[str, int]] = []
        self.resets = 0

    def step_cursor(self, *, timeframe, bars):
        self.steps.append((timeframe, bars))

    def reset_cursor(self):
        self.resets += 1


@pytest.fixture
def service(monkeypatch):
    stub = StubService()
    monkeypatch.setattr(market_xray, "_app_service", lambda _app: stub)
    return stub


def frames(monkeypatch, *, seconds=0.0, on_start=None):
    """Replaces the frame build, and records when builds start and finish."""
    log = {"started": 0, "finished": 0}

    def build(_service, timeframe):
        log["started"] += 1
        if on_start:
            on_start()
        time.sleep(seconds)
        log["finished"] += 1
        return {"type": "market.frame", "timeframe": timeframe}

    monkeypatch.setattr(market_xray, "_build_frame", build)
    return log


def run(websocket, timeout=5.0):
    return asyncio.wait_for(market_xray.market_ws(websocket), timeout=timeout)


# ---- the reported defect ------------------------------------------------


async def test_a_disconnect_during_a_build_never_sends(service, monkeypatch):
    """The sender used to wake from the build and write without looking."""
    frames(monkeypatch, seconds=0.05)
    socket_ = FakeSocket([WebSocketDisconnect(code=1000)])
    await run(socket_)
    assert socket_.sent == []


async def test_the_server_closing_first_ends_the_connection_quietly(service, monkeypatch):
    """A check before sending cannot see a close the server has already done
    and the receiver has not been scheduled to notice. So the write itself has
    to treat that as the end of the connection."""
    frames(monkeypatch)
    socket_ = FakeSocket()
    socket_.send_error = RuntimeError(UVICORN_CLOSED_ERROR)
    await run(socket_)
    assert socket_.sent == []


async def test_starlettes_own_closed_state_is_recognised(service, monkeypatch):
    frames(monkeypatch)
    socket_ = FakeSocket()
    socket_.send_error = RuntimeError('Cannot call "send" once a close message has been sent.')
    await run(socket_)
    assert socket_.closed_with == []


async def test_a_disconnect_reported_by_the_write_is_normal(service, monkeypatch):
    frames(monkeypatch)
    socket_ = FakeSocket()
    socket_.send_error = WebSocketDisconnect(code=1006)
    await run(socket_)
    assert socket_.closed_with == [], "a gone client must not be closed again"


# ---- nothing is left behind ---------------------------------------------


async def test_the_handler_does_not_finish_before_its_build(service, monkeypatch):
    """`to_thread` cannot be cancelled, so returning early would leave a worker
    running with nothing attached to it."""
    log = frames(monkeypatch, seconds=0.15)
    await run(FakeSocket([WebSocketDisconnect(code=1000)]))
    assert log == {"started": 1, "finished": 1}


async def test_no_new_build_starts_after_a_disconnect(service, monkeypatch):
    log = frames(monkeypatch, seconds=0.05)
    await run(FakeSocket([WebSocketDisconnect(code=1000)]))
    await asyncio.sleep(0.1)
    assert log["started"] == 1


async def test_no_tasks_outlive_the_handler(service, monkeypatch):
    frames(monkeypatch, seconds=0.05)
    before = asyncio.all_tasks()
    await run(FakeSocket([WebSocketDisconnect(code=1000)]))
    await asyncio.sleep(0)
    assert asyncio.all_tasks() - before == set()


async def test_cancellation_propagates_after_cleaning_up(service, monkeypatch):
    log = frames(monkeypatch, seconds=0.15)
    started = threading.Event()
    monkeypatch.setattr(
        market_xray, "_build_frame",
        lambda s, t, build=market_xray._build_frame: (started.set(), build(s, t))[1],
    )
    task = asyncio.create_task(market_xray.market_ws(FakeSocket()))
    while not started.is_set():
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert log["finished"] == 1, "the build was abandoned, not waited for"


async def test_a_build_failure_after_disconnect_is_retrieved(service, monkeypatch, caplog):
    """Otherwise it surfaces as 'Task exception was never retrieved'."""

    def boom(_service, _timeframe):
        time.sleep(0.05)
        raise ValueError("bad store")

    monkeypatch.setattr(market_xray, "_build_frame", boom)
    with caplog.at_level(logging.WARNING, logger="tars.market.api"):
        await run(FakeSocket([WebSocketDisconnect(code=1000)]))
    assert "bad store" in caplog.text


# ---- unrelated errors stay loud -----------------------------------------


async def test_an_unrelated_send_error_is_not_mistaken_for_a_disconnect(service, monkeypatch):
    frames(monkeypatch)
    socket_ = FakeSocket()
    socket_.send_error = RuntimeError("a genuine bug")
    with pytest.raises(RuntimeError, match="a genuine bug"):
        await run(socket_)


async def test_an_unserialisable_frame_is_not_swallowed(service, monkeypatch):
    frames(monkeypatch)
    socket_ = FakeSocket()
    socket_.send_error = TypeError("not JSON serialisable")
    with pytest.raises(TypeError):
        await run(socket_)


async def test_a_bug_in_the_receiver_surfaces_and_closes_once(service, monkeypatch):
    frames(monkeypatch)
    socket_ = FakeSocket([AttributeError("a genuine bug")])
    with pytest.raises(AttributeError):
        await run(socket_)
    assert socket_.closed_with == [1011]


# ---- closing ------------------------------------------------------------


@pytest.mark.parametrize(
    "client_message",
    [ValueError("not JSON"), {"type": "replay.step", "bars": "many"}, ["not", "an", "object"]],
)
async def test_a_bad_client_message_closes_exactly_once(service, monkeypatch, client_message):
    frames(monkeypatch)
    socket_ = FakeSocket([client_message])
    await run(socket_)
    assert socket_.closed_with == [1008]


async def test_a_close_racing_a_dead_socket_is_harmless(service, monkeypatch):
    frames(monkeypatch)

    class Dying(FakeSocket):
        async def close(self, code=1000, reason=None):
            self.closed_with.append(code)
            raise RuntimeError(UVICORN_CLOSED_ERROR)

    socket_ = Dying([{"type": "replay.step", "bars": "many"}])
    await run(socket_)
    assert socket_.closed_with == [1008]


async def test_store_unavailable_reports_and_closes_once(monkeypatch):
    def unavailable(_app):
        raise RuntimeError("no store")

    monkeypatch.setattr(market_xray, "_app_service", unavailable)
    socket_ = FakeSocket()
    await run(socket_)
    assert [f["type"] for f in socket_.sent] == ["market.error"]
    assert socket_.sent[0]["error"] == "market_store_unavailable"
    assert socket_.closed_with == [1000]


async def test_store_unavailable_with_the_client_already_gone(monkeypatch):
    def unavailable(_app):
        raise RuntimeError("no store")

    monkeypatch.setattr(market_xray, "_app_service", unavailable)
    socket_ = FakeSocket()
    socket_.send_error = RuntimeError(UVICORN_CLOSED_ERROR)
    await run(socket_)
    assert socket_.closed_with == [], "there is nothing left to close"


# ---- the message contract is unchanged ----------------------------------


async def test_frames_still_arrive_and_follow_a_subscription(service, monkeypatch):
    frames(monkeypatch)
    monkeypatch.setattr(market_xray, "PUSH_INTERVAL_SECONDS", 0.01)
    socket_ = FakeSocket()
    task = asyncio.create_task(market_xray.market_ws(socket_))
    while not socket_.sent:
        await asyncio.sleep(0.005)
    assert socket_.sent[0] == {"type": "market.frame", "timeframe": universe_default()}

    socket_.script.append({"type": "subscribe", "timeframe": "m15"})
    await asyncio.sleep(0.1)
    assert socket_.sent[-1]["timeframe"] == "M15"
    socket_.script.append(WebSocketDisconnect(code=1000))
    await asyncio.wait_for(task, 5)


async def test_an_unknown_timeframe_is_ignored_as_before(service, monkeypatch):
    frames(monkeypatch)
    monkeypatch.setattr(market_xray, "PUSH_INTERVAL_SECONDS", 0.01)
    socket_ = FakeSocket([{"type": "subscribe", "timeframe": "W1"}])
    task = asyncio.create_task(market_xray.market_ws(socket_))
    await asyncio.sleep(0.06)
    socket_.script.append(WebSocketDisconnect(code=1000))
    await asyncio.wait_for(task, 5)
    assert socket_.sent[-1]["timeframe"] == universe_default()


async def test_a_build_failure_is_still_reported_as_a_frame(service, monkeypatch):
    """The dashboard shows this; it must not end the connection."""
    calls = []

    def boom(_service, _timeframe):
        calls.append(1)
        raise ValueError("bad store")

    monkeypatch.setattr(market_xray, "_build_frame", boom)
    monkeypatch.setattr(market_xray, "PUSH_INTERVAL_SECONDS", 0.01)
    socket_ = FakeSocket()
    task = asyncio.create_task(market_xray.market_ws(socket_))
    while len(socket_.sent) < 2:
        await asyncio.sleep(0.005)
    socket_.script.append(WebSocketDisconnect(code=1000))
    await asyncio.wait_for(task, 5)
    assert socket_.sent[0]["type"] == "market.error"
    assert socket_.sent[0]["message"] == "bad store"
    assert "sent_at" in socket_.sent[0]


async def test_replay_commands_still_reach_the_service(service, monkeypatch):
    frames(monkeypatch)
    socket_ = FakeSocket([
        {"type": "subscribe", "timeframe": "H4"},
        {"type": "replay.step", "bars": -3},
        {"type": "replay.reset"},
        WebSocketDisconnect(code=1000),
    ])
    await run(socket_)
    assert service.steps == [("H4", -3)]
    assert service.resets == 1


async def test_replay_commands_with_an_empty_store_do_not_end_the_connection(service, monkeypatch):
    frames(monkeypatch)
    service.bounds = (None, None)
    socket_ = FakeSocket([{"type": "replay.step", "bars": 1}, {"type": "replay.reset"},
                          WebSocketDisconnect(code=1000)])
    await run(socket_)
    assert service.steps == [] and service.resets == 0
    assert socket_.closed_with == []


def universe_default() -> str:
    return market_xray.universe.DEFAULT_TIMEFRAME


# ---- a real server, really aborted --------------------------------------


class ErrorCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.records: list[str] = []

    def emit(self, record):
        text = record.getMessage()
        if record.exc_info:
            text += " " + repr(record.exc_info[1])
        self.records.append(text)


@pytest.fixture
def live_server(monkeypatch):
    stub = StubService()
    monkeypatch.setattr(market_xray, "_app_service", lambda _app: stub)
    active = {"builds": 0, "peak": 0, "total": 0}
    lock = threading.Lock()

    def build(_service, timeframe):
        with lock:
            active["builds"] += 1
            active["total"] += 1
            active["peak"] = max(active["peak"], active["builds"])
        time.sleep(0.03)
        with lock:
            active["builds"] -= 1
        return {"type": "market.frame", "timeframe": timeframe, "padding": "x" * 20000}

    monkeypatch.setattr(market_xray, "_build_frame", build)
    monkeypatch.setattr(market_xray, "PUSH_INTERVAL_SECONDS", 0.01)

    app = FastAPI()
    app.include_router(market_xray.router)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, ws="websockets")
    )
    errors = ErrorCapture()
    logging.getLogger("uvicorn.error").addHandler(errors)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started

    yield f"ws://127.0.0.1:{port}/api/v1/market/ws", server, errors, active

    server.should_exit = True
    thread.join(timeout=15)
    logging.getLogger("uvicorn.error").removeHandler(errors)


async def _abort_at(url: str, delay: float) -> None:
    connection = await websockets.connect(url, max_size=None)
    await asyncio.sleep(delay)
    connection.transport.abort()


async def _settle(server, active) -> None:
    deadline = time.time() + 10
    while time.time() < deadline:
        if not server.server_state.connections and not server.server_state.tasks and not active["builds"]:
            return
        await asyncio.sleep(0.02)


def assert_clean(errors, server, active) -> None:
    assert errors.records == [], errors.records[:3]
    assert not server.server_state.connections, "connections left open"
    assert not server.server_state.tasks, "handler tasks left running"
    assert active["builds"] == 0, "a frame build is still running"


async def test_stress_aborting_at_every_phase_of_a_frame(live_server):
    """Delays straddle the 30ms build and the 10ms wait, so aborts land before,
    during and after a build, and during a large write."""
    url, server, errors, active = live_server
    for _ in range(4):
        await asyncio.gather(*[_abort_at(url, 0.002 * i) for i in range(40)])
    await _settle(server, active)
    assert active["total"] > 0, "nothing was exercised"
    assert_clean(errors, server, active)


async def test_stress_a_clean_close_during_a_build(live_server):
    url, server, errors, active = live_server

    async def polite(delay):
        connection = await websockets.connect(url, max_size=None)
        await asyncio.sleep(delay)
        await connection.close()

    await asyncio.gather(*[polite(0.003 * i) for i in range(40)])
    await _settle(server, active)
    assert_clean(errors, server, active)


async def test_a_real_client_still_receives_frames_and_a_clean_close(live_server):
    url, server, errors, active = live_server
    async with websockets.connect(url, max_size=None) as connection:
        first = json.loads(await asyncio.wait_for(connection.recv(), 5))
        assert first["type"] == "market.frame"
        await connection.send(json.dumps({"type": "subscribe", "timeframe": "M5"}))
        for _ in range(20):
            frame = json.loads(await asyncio.wait_for(connection.recv(), 5))
            if frame["timeframe"] == "M5":
                break
        assert frame["timeframe"] == "M5"
    await _settle(server, active)
    assert_clean(errors, server, active)


async def test_invalid_json_from_a_real_client_gets_a_policy_close(live_server):
    url, server, errors, active = live_server
    connection = await websockets.connect(url, max_size=None)
    await connection.send("this is not json")
    with pytest.raises(websockets.ConnectionClosed) as closed:
        while True:
            await asyncio.wait_for(connection.recv(), 5)
    assert closed.value.rcvd is not None and closed.value.rcvd.code == 1008
    await _settle(server, active)
    assert_clean(errors, server, active)
