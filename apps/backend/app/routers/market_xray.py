"""Market X-Ray HTTP + WebSocket API.

The backend is the single source of truth: every number in these responses is
computed here from the recorded candle store, and the frontend renders them
without recalculating anything.

The service is created lazily on first use and cached on ``app.state`` so a
missing candle store degrades to a truthful 503 describing how to build it,
rather than preventing the whole backend from starting.

**Why these handlers are `def` and not `async def`.** Every one of them does
synchronous, CPU-bound work: DuckDB scans, ATR passes, order-block detection
across the universe. On the event loop that would block *every* other request
for the duration -- including the desktop shell's backend health poll, which
then concludes the backend has died and restarts it. Declaring them `def`
makes Starlette run each in its threadpool, so the loop stays responsive. The
WebSocket handler must stay `async`, so it offloads the same work with
``asyncio.to_thread``, and its frames are built by `market.frames`, which
shares them between connections.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect

from market import feed, ingest, setup, universe
from market.frames import Flight, SharedFrames
from market.service import FrameInputs, MarketXRayService
from market.store import CandleStore, RevisionChanged, active_dataset_revision

logger = logging.getLogger("tars.market.api")

router = APIRouter(prefix="/api/v1/market", tags=["market-xray"])

# How often a connected dashboard is pushed a fresh status/x-ray frame.
# Each frame costs real CPU (see `_build_frame`), so this is deliberately
# unhurried: the underlying data is recorded history, not a ticking feed.
PUSH_INTERVAL_SECONDS = 10.0
# How long a disconnect waits for a frame already being built before giving up.
FRAME_DRAIN_SECONDS = 60.0
# Finished frames kept. Enough for every timeframe at the current cursor and a
# few replay steps around it; each is a few hundred kilobytes.
FRAMES_RETAINED = 16
# The panels of a frame, each of which carries its own provenance.
_FRAME_PANELS = ("xray", "strength", "leaders")
# WebSocket close codes.
_POLICY_VIOLATION = 1008
_INTERNAL_ERROR = 1011
# How uvicorn and Starlette say a socket is already closed.
_CLOSED_SOCKET_MESSAGES = (
    "after sending 'websocket.close'",
    "once a close message has been sent",
)

_frames = SharedFrames(retained=FRAMES_RETAINED)
# Clients arriving together at a fresh backend must not each build a service.
_service_creation = threading.Lock()


def _service(request: Request) -> MarketXRayService:
    return _app_service(request.app)


def _app_service(app) -> MarketXRayService:
    existing = getattr(app.state, "market_xray_service", None)
    if existing is not None and (
        existing.store.recorded_revision == active_dataset_revision(existing.store.root)
    ):
        return existing
    with _service_creation:
        existing = getattr(app.state, "market_xray_service", None)
        if existing is not None:
            if existing.store.recorded_revision == active_dataset_revision(existing.store.root):
                return existing
            # An external import atomically published a new generation.
            # Keep the old immutable store alive for any in-flight build,
            # and replace only the service pointer used by new requests.
            replacement = MarketXRayService(
                store=CandleStore(existing.store.root),
                state=existing.state,
                config=existing.config,
            )
            replacement.adopt_feed_from(existing)
            if existing.replay_pinned and replacement.bounds[1] is not None:
                replacement.set_cursor(existing.cursor)
            app.state.market_xray_service = replacement
            return replacement
        try:
            service = MarketXRayService()
        except Exception as exc:  # noqa: BLE001
            logger.warning("market x-ray service unavailable: %s", exc)
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "market_store_unavailable",
                    "message": str(exc),
                    "remedy": "Build the candle store: python -m market.ingest",
                },
            ) from exc
        app.state.market_xray_service = service
        _start_feed(app)
        return service


def _start_feed(app) -> None:
    """Bridge the existing MT5 monitor to the service, once. With MT5 disabled
    there is simply no bridge, and the dashboard runs on recorded history."""
    if getattr(app.state, "market_feed", None) is not None:
        return
    monitors = getattr(app.state, "monitors", None)
    provider = getattr(monitors, "mt5", None)
    if provider is None:
        return
    bridge = feed.FeedBridge(lambda: getattr(app.state, "market_xray_service", None), provider)
    app.state.market_feed = bridge
    bridge.start()


def _timeframe(value: str) -> str:
    tf = value.upper()
    if tf not in universe.TIMEFRAMES:
        raise HTTPException(
            status_code=400,
            detail="unknown timeframe " + value + "; expected one of "
            + ", ".join(universe.TIMEFRAMES),
        )
    return tf


def _symbol(service: MarketXRayService, value: str) -> str:
    sym = value.upper().replace("/", "")
    if sym not in service.store.available_symbols:
        raise HTTPException(
            status_code=404,
            detail="no data for " + value + "; available: "
            + ", ".join(service.store.available_symbols),
        )
    return sym


def _import_job(request: Request) -> setup.ImportJob:
    job = getattr(request.app.state, "market_import_job", None)
    if job is None:
        def reload_service() -> None:
            # The service reads its bounds at construction, so a fresh import
            # needs a fresh service. Replaced, not mutated, so a request in
            # flight finishes against the object it started with.
            previous = _service(request)
            replacement = MarketXRayService(
                store=CandleStore(previous.store.root), state=previous.state
            )
            replacement.adopt_feed_from(previous)
            request.app.state.market_xray_service = replacement

        job = setup.ImportJob(on_complete=reload_service)
        request.app.state.market_import_job = job
    return job


@router.get("/setup")
def setup_status(request: Request, csv_root: str | None = Query(None)) -> dict:
    """What the dashboard has to work with, for the first-run screen. Answers
    with an empty store, which is the case every other route cannot serve."""
    # In this order: an import that reports DONE has already swapped the
    # service in, so the reverse could pair DONE with the old, empty store.
    import_state = _import_job(request).status()
    service = _service(request)
    root = Path(csv_root) if csv_root else ingest.csv_root()
    return {
        "ready": service.bounds[1] is not None,
        "store_root": str(service.store.root),
        "symbols_in_store": len(service.store.available_symbols),
        "sources": setup.discover_sources(root),
        "coverage": universe.coverage_summary(),
        "import": import_state,
        "status": service.market_status().as_dict(),
        "trading_execution": "DISABLED",
    }


@router.post("/import")
def start_import(request: Request, csv_root: str | None = Query(None)) -> dict:
    """Build the candle store from local CSVs, in the background."""
    root = Path(csv_root) if csv_root else ingest.csv_root()
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"{root} is not a folder on this machine")
    service = _service(request)
    return _import_job(request).start(csv_root=root, store_root=service.store.root)


@router.get("/health")
def health(request: Request) -> dict:
    """Feed health and store coverage. Reports the truth about staleness."""
    service = _service(request)
    first, last = service.bounds
    return {
        "ok": True,
        "status": service.market_status().as_dict(),
        "feed": service.feed_state,
        "store": {
            "root": str(service.store.root),
            "symbols": len(service.store.available_symbols),
            "first_bar": first.isoformat() if first else None,
            "last_bar": last.isoformat() if last else None,
            "manifest_generated_at": service.store.manifest.get("generated_at"),
            "source_csv_root": service.store.manifest.get("csv_root"),
        },
        "persisted": {
            "zones": service.state.zone_count(),
            "events": service.state.event_count(),
        },
        "config_version": service.config.version,
        "trading_execution": "DISABLED",
    }


@router.get("/instruments")
def instruments(request: Request) -> dict:
    service = _service(request)
    return {
        "instruments": service.instruments(),
        "currencies": list(universe.CURRENCIES),
        "timeframes": universe.TIMEFRAMES,
        "requested_universe_note": (
            "17 FX pairs plus XAUUSD are backed by recorded bid/ask data on this "
            "machine. The 8 further majors named in the brief (USDCHF, NZDJPY, "
            "NZDCAD, NZDCHF, EURNZD, GBPNZD, AUDNZD, CADJPY/CADCHF) have no "
            "source files here and are therefore not offered."
        ),
    }


@router.get("/dashboard")
def dashboard(
    request: Request,
    timeframe: str = Query(universe.DEFAULT_TIMEFRAME),
    symbol: str | None = Query(None),
    strength_window: str | None = Query(None),
) -> dict:
    service = _service(request)
    tf = _timeframe(timeframe)
    sym = _symbol(service, symbol) if symbol else None
    return service.dashboard(timeframe=tf, symbol=sym, strength_window=strength_window)


@router.get("/xray")
def xray(request: Request, timeframe: str = Query(universe.DEFAULT_TIMEFRAME)) -> dict:
    service = _service(request)
    return service.xray(timeframe=_timeframe(timeframe))


@router.get("/strength")
def strength(request: Request, window: str | None = Query(None)) -> dict:
    service = _service(request)
    try:
        return service.strength(window=window)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/strength/{currency}/history")
def strength_history(
    request: Request, currency: str, window: str = Query("1D"), limit: int = Query(200, le=1000)
) -> dict:
    service = _service(request)
    cur = currency.upper()
    if cur not in universe.CURRENCIES:
        raise HTTPException(status_code=404, detail="unknown currency " + currency)
    return {
        "currency": cur,
        "window": window,
        "history": service.state.strength_history(cur, window, limit),
    }


@router.get("/leaders")
def leaders(
    request: Request,
    timeframe: str = Query(universe.DEFAULT_TIMEFRAME),
    limit: int = Query(6, ge=1, le=20),
) -> dict:
    service = _service(request)
    return service.leaders(timeframe=_timeframe(timeframe), limit=limit)


@router.get("/candles/{symbol}")
def candles(
    request: Request,
    symbol: str,
    timeframe: str = Query(universe.DEFAULT_TIMEFRAME),
    limit: int = Query(300, ge=10, le=2000),
) -> dict:
    service = _service(request)
    return service.candles(
        symbol=_symbol(service, symbol), timeframe=_timeframe(timeframe), limit=limit
    )


@router.get("/order-blocks/{symbol}")
def order_blocks(
    request: Request, symbol: str, timeframe: str = Query(universe.DEFAULT_TIMEFRAME)
) -> dict:
    service = _service(request)
    return service.order_blocks(
        symbol=_symbol(service, symbol), timeframe=_timeframe(timeframe)
    )


@router.get("/events")
def events(
    request: Request, symbol: str | None = Query(None), limit: int = Query(100, ge=1, le=1000)
) -> dict:
    service = _service(request)
    return service.events(symbol=symbol.upper() if symbol else None, limit=limit)


@router.get("/config")
def config(request: Request) -> dict:
    return _service(request).config.as_dict()


# ---- replay control -----------------------------------------------------
@router.post("/replay/seek")
def replay_seek(request: Request, to: str = Query(..., description="ISO-8601 UTC")) -> dict:
    """Point the dashboard at a specific recorded moment."""
    service = _service(request)
    try:
        target = datetime.fromisoformat(to)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="`to` must be ISO-8601: " + str(exc)) from exc
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    cursor = service.set_cursor(target)
    return {"cursor": cursor.isoformat(), "status": service.market_status().as_dict()}


@router.post("/replay/step")
def replay_step(
    request: Request,
    timeframe: str = Query(universe.DEFAULT_TIMEFRAME),
    bars: int = Query(1, ge=-5000, le=5000),
) -> dict:
    """Advance (or rewind, with a negative value) by whole bars."""
    service = _service(request)
    cursor = service.step_cursor(timeframe=_timeframe(timeframe), bars=bars)
    return {"cursor": cursor.isoformat(), "status": service.market_status().as_dict()}


@router.post("/replay/reset")
def replay_reset(request: Request) -> dict:
    service = _service(request)
    cursor = service.reset_cursor()
    return {"cursor": cursor.isoformat(), "status": service.market_status().as_dict()}


def _build_frame(inputs: FrameInputs, timeframe: str) -> dict:
    """The panels of a WebSocket frame, computed from `inputs`. Synchronous by
    design -- callers run it in a thread so the event loop stays free.

    The result is shared and must not be changed. It holds what follows from
    `inputs` alone: `_labelled` adds whatever depends on the moment of sending.
    """
    service = inputs.service
    # One coherent DuckDB revision for every panel. All store queries use
    # its reentrant lock; a concurrent feed batch must wait until the frame
    # is complete. Stale builds retry rather than claim the old revision.
    if hasattr(service, "store"):
        with service.store.consistent_snapshot(inputs.data_revision):
            return _build_frame_panels(inputs, timeframe)
    return _build_frame_panels(inputs, timeframe)


def _build_frame_panels(inputs: FrameInputs, timeframe: str) -> dict:
    service = inputs.service
    service.market_status()
    if inputs.as_of is None:
        # Nothing to compute from yet: the first-run screen owns this state,
        # and a frame must not fail just because the store is still empty.
        return {"type": "market.status"}
    return {
        "type": "market.frame",
        "cursor": inputs.as_of.isoformat(),
        "timeframe": timeframe,
        "data_revision": inputs.data_revision,
        "dataset_revision": getattr(getattr(service, "store", None), "recorded_revision", None),
        "xray": service.xray(timeframe=timeframe, as_of=inputs.as_of),
        "strength": service.strength(as_of=inputs.as_of),
        "leaders": service.leaders(timeframe=timeframe, as_of=inputs.as_of),
    }


def _labelled(frame: dict, labels: dict) -> dict:
    """A copy of a shared frame for sending now, under the labels of now."""
    if frame["type"] == "market.status":
        return {**frame, "status": labels["status"], "feed": labels["feed"]}
    sending = {**frame, **labels}
    for panel in _FRAME_PANELS:
        if panel in frame:
            sending[panel] = {**frame[panel], "provenance": labels["provenance"]}
    return sending


# ---- websocket ----------------------------------------------------------
class _MarketSocket:
    """The only route to one connection's socket.

    A client can vanish at any moment, including while a frame is being built
    in a thread, and checking a flag before writing cannot close that gap: the
    server can have closed the connection before the receiver task has been
    scheduled to notice. So writes are serialised here, each one checks under
    the lock, and a write that finds the connection gone reports that instead
    of raising. Only disconnects are treated this way; any other error from a
    write is a bug and propagates.
    """

    def __init__(self, websocket: WebSocket):
        self._websocket = websocket
        self._write = asyncio.Lock()
        self._closed = False
        # Set when frames should stop: the peer left, or the receiver ended.
        self.stopped = asyncio.Event()

    def peer_gone(self) -> None:
        self._closed = True
        self.stopped.set()

    async def send(self, frame: dict) -> bool:
        """False if the connection is over, in which case nothing was sent."""
        async with self._write:
            if self._closed or self.stopped.is_set():
                return False
            try:
                await self._websocket.send_json(frame)
            except WebSocketDisconnect:
                self.peer_gone()
                return False
            except RuntimeError as exc:
                if not _is_closed_socket_error(exc):
                    raise
                self.peer_gone()
                return False
            return True

    async def close(self, code: int = 1000) -> None:
        """Close once. A connection that is already over needs nothing."""
        async with self._write:
            if self._closed:
                return
            self._closed = True
            self.stopped.set()
            try:
                await self._websocket.close(code)
            except WebSocketDisconnect:
                pass
            except RuntimeError as exc:
                if not _is_closed_socket_error(exc):
                    raise


def _is_closed_socket_error(exc: RuntimeError) -> bool:
    """Whether this is the server or Starlette refusing a write to a closed
    socket. Neither has an exception type of its own for it, so the wording is
    all there is, and it is matched narrowly so a real bug is never absorbed."""
    return any(marker in str(exc) for marker in _CLOSED_SOCKET_MESSAGES)


async def _finished_before(task: asyncio.Future, stop: asyncio.Event) -> bool:
    """Wait for `task`, or for `stop`. True only if the result is still wanted."""
    stopping = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({task, stopping}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopping.cancel()
    return not stop.is_set()


async def _drain_build(building: asyncio.Future | None) -> None:
    """See a frame build through after the connection has ended.

    A running build cannot be cancelled, so returning now would leave it
    running with nothing attached; one that never started is already over.
    Bounded, because a stuck query must not pin the handler forever. Its
    result is unwanted here, but a failure is still reported.
    """
    if building is None:
        return
    if not building.done():
        await asyncio.wait({building}, timeout=FRAME_DRAIN_SECONDS)
    if not building.done():
        logger.warning("market ws frame build still running after %ss", FRAME_DRAIN_SECONDS)
    elif not building.cancelled() and building.exception() is not None:
        logger.warning("market ws frame build failed after disconnect: %s", building.exception())


@router.websocket("/ws")
async def market_ws(websocket: WebSocket) -> None:
    """Pushes `market.frame` messages: status plus the current X-Ray, strength
    and leaders for the subscribed timeframe.

    Each frame carries the cursor it was computed at, so a client can tell a
    genuinely new frame from a repeat and never has to guess whether what it
    is showing is current.
    """
    await websocket.accept()
    connection = _MarketSocket(websocket)
    try:
        await asyncio.to_thread(_app_service, websocket.app)
    except Exception as exc:  # noqa: BLE001
        await connection.send({
            "type": "market.error",
            "error": "market_store_unavailable",
            "message": getattr(exc, "detail", None) or str(exc),
        })
        await connection.close()
        return

    def service() -> MarketXRayService:
        # Looked up each time: an import replaces the service mid-connection.
        return _app_service(websocket.app)

    timeframe = universe.DEFAULT_TIMEFRAME
    initial_subscription = asyncio.Event()

    async def handle(msg: object) -> None:
        nonlocal timeframe
        if not isinstance(msg, dict):
            raise ValueError("expected a JSON object")
        kind = msg.get("type")
        if kind == "subscribe":
            requested = str(msg.get("timeframe", timeframe)).upper()
            if requested in universe.TIMEFRAMES:
                timeframe = requested
                initial_subscription.set()
        elif kind == "replay.step":
            bars = int(msg.get("bars", 1))
            if service().bounds[1] is not None:
                await asyncio.to_thread(service().step_cursor, timeframe=timeframe, bars=bars)
        elif kind == "replay.reset":
            if service().bounds[1] is not None:
                await asyncio.to_thread(service().reset_cursor)

    async def receive_loop() -> None:
        """Client control messages: change timeframe, or drive the replay.

        Ends on a disconnect or on a message that cannot be understood. Any
        other exception is a bug, and reaches the end of `market_ws`.
        """
        while True:
            try:
                msg = await websocket.receive_json()
            except WebSocketDisconnect:
                connection.peer_gone()
                return
            except (KeyError, TypeError, ValueError) as exc:
                # A binary frame, or text that is not JSON.
                logger.info("market ws closing on an unreadable message: %s", exc)
                await connection.close(_POLICY_VIOLATION)
                return
            try:
                await handle(msg)
            except (TypeError, ValueError) as exc:
                logger.info("market ws closing on an invalid command: %s", exc)
                await connection.close(_POLICY_VIOLATION)
                return

    flight: Flight | None = None
    building: asyncio.Future | None = None

    async def next_frame() -> dict | None:
        """Build under one dataset/live revision, retrying if it changes.

        The service can be replaced by an external import, a feed batch can
        commit while a build waits, and a subscribe/replay command can change
        the requested view. None of those may send the old frame as current.
        """
        nonlocal flight, building
        for _attempt in range(4):
            current = await asyncio.to_thread(service)
            inputs = current.frame_inputs(timeframe)
            flight = _frames.join(inputs, lambda: _build_frame(inputs, inputs.timeframe))
            building = asyncio.wrap_future(flight.work)
            if not await _finished_before(building, connection.stopped):
                return None
            finished = building
            _frames.leave(flight)
            flight = building = None
            try:
                frame = finished.result()
            except RevisionChanged:
                continue

            latest = await asyncio.to_thread(service)
            if latest is not inputs.service or timeframe != inputs.timeframe:
                continue
            if hasattr(latest, "store") and latest.frame_inputs(timeframe) != inputs:
                continue
            return _labelled(frame, inputs.service.frame_labels(inputs))
        raise RuntimeError("market data changed repeatedly while preparing a frame")

    receiver = asyncio.create_task(receive_loop())
    # However it ends, a finished receiver means nothing is listening.
    receiver.add_done_callback(lambda _task: connection.stopped.set())
    # Give the immediate on-open subscribe command priority over a default
    # H1 frame. If a subscription arrives while building, next_frame retries
    # under its timeframe rather than publishing a wrongly selected H1 frame.
    try:
        await asyncio.wait_for(initial_subscription.wait(), timeout=0.1)
    except TimeoutError:
        pass
    try:
        while not connection.stopped.is_set():
            try:
                frame = await next_frame()
            except Exception as exc:  # noqa: BLE001
                frame = {
                    "type": "market.error",
                    "sent_at": datetime.now(UTC).isoformat(),
                    "message": str(exc),
                }
            if frame is None or not await connection.send(frame):
                break
            try:
                await asyncio.wait_for(connection.stopped.wait(), timeout=PUSH_INTERVAL_SECONDS)
            except TimeoutError:
                pass
    finally:
        connection.stopped.set()
        receiver.cancel()
        outcome = (await asyncio.gather(receiver, return_exceptions=True))[0]
        if flight is not None:
            _frames.leave(flight)
        await _drain_build(building)
    if isinstance(outcome, Exception):
        await connection.close(_INTERNAL_ERROR)
        raise outcome
