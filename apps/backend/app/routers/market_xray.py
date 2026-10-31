"""Market X-Ray HTTP + WebSocket API.

The backend is the single source of truth: every number in these responses is
computed here from the recorded candle store, and the frontend renders them
without recalculating anything.

The service is created lazily on first use and cached on ``app.state`` so a
missing candle store degrades to a truthful 503 describing how to build it,
rather than preventing the whole backend from starting.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect

from market import universe
from market.service import MarketXRayService

logger = logging.getLogger("tars.market.api")

router = APIRouter(prefix="/api/v1/market", tags=["market-xray"])

# How often a connected dashboard is pushed a fresh status/x-ray frame.
PUSH_INTERVAL_SECONDS = 5.0


def _service(request: Request) -> MarketXRayService:
    existing = getattr(request.app.state, "market_xray_service", None)
    if existing is not None:
        return existing
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
    request.app.state.market_xray_service = service
    return service


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


@router.get("/health")
async def health(request: Request) -> dict:
    """Feed health and store coverage. Reports the truth about staleness."""
    service = _service(request)
    first, last = service.bounds
    return {
        "ok": True,
        "status": service.market_status().as_dict(),
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
async def instruments(request: Request) -> dict:
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
async def dashboard(
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
async def xray(request: Request, timeframe: str = Query(universe.DEFAULT_TIMEFRAME)) -> dict:
    service = _service(request)
    return service.xray(timeframe=_timeframe(timeframe))


@router.get("/strength")
async def strength(request: Request, window: str | None = Query(None)) -> dict:
    service = _service(request)
    try:
        return service.strength(window=window)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/strength/{currency}/history")
async def strength_history(
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
async def leaders(
    request: Request,
    timeframe: str = Query(universe.DEFAULT_TIMEFRAME),
    limit: int = Query(6, ge=1, le=20),
) -> dict:
    service = _service(request)
    return service.leaders(timeframe=_timeframe(timeframe), limit=limit)


@router.get("/candles/{symbol}")
async def candles(
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
async def order_blocks(
    request: Request, symbol: str, timeframe: str = Query(universe.DEFAULT_TIMEFRAME)
) -> dict:
    service = _service(request)
    return service.order_blocks(
        symbol=_symbol(service, symbol), timeframe=_timeframe(timeframe)
    )


@router.get("/events")
async def events(
    request: Request, symbol: str | None = Query(None), limit: int = Query(100, ge=1, le=1000)
) -> dict:
    service = _service(request)
    return service.events(symbol=symbol.upper() if symbol else None, limit=limit)


@router.get("/config")
async def config(request: Request) -> dict:
    return _service(request).config.as_dict()


# ---- replay control -----------------------------------------------------
@router.post("/replay/seek")
async def replay_seek(request: Request, to: str = Query(..., description="ISO-8601 UTC")) -> dict:
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
async def replay_step(
    request: Request,
    timeframe: str = Query(universe.DEFAULT_TIMEFRAME),
    bars: int = Query(1, ge=-5000, le=5000),
) -> dict:
    """Advance (or rewind, with a negative value) by whole bars."""
    service = _service(request)
    cursor = service.step_cursor(timeframe=_timeframe(timeframe), bars=bars)
    return {"cursor": cursor.isoformat(), "status": service.market_status().as_dict()}


@router.post("/replay/reset")
async def replay_reset(request: Request) -> dict:
    service = _service(request)
    cursor = service.reset_cursor()
    return {"cursor": cursor.isoformat(), "status": service.market_status().as_dict()}


# ---- websocket ----------------------------------------------------------
@router.websocket("/ws")
async def market_ws(websocket: WebSocket) -> None:
    """Pushes `market.frame` messages: status plus the current X-Ray, strength
    and leaders for the subscribed timeframe.

    Each frame carries the cursor it was computed at, so a client can tell a
    genuinely new frame from a repeat and never has to guess whether what it
    is showing is current.
    """
    await websocket.accept()
    try:
        service = MarketXRayService()
    except Exception as exc:  # noqa: BLE001
        await websocket.send_json({
            "type": "market.error",
            "error": "market_store_unavailable",
            "message": str(exc),
            "remedy": "Build the candle store: python -m market.ingest",
        })
        await websocket.close()
        return

    cached = getattr(websocket.app.state, "market_xray_service", None)
    if cached is not None:
        service = cached
    else:
        websocket.app.state.market_xray_service = service

    timeframe = universe.DEFAULT_TIMEFRAME
    stop = asyncio.Event()

    async def receive_loop() -> None:
        """Client control messages: change timeframe, or drive the replay."""
        nonlocal timeframe
        try:
            while True:
                msg = await websocket.receive_json()
                kind = msg.get("type")
                if kind == "subscribe":
                    requested = str(msg.get("timeframe", timeframe)).upper()
                    if requested in universe.TIMEFRAMES:
                        timeframe = requested
                elif kind == "replay.step":
                    service.step_cursor(timeframe=timeframe, bars=int(msg.get("bars", 1)))
                elif kind == "replay.reset":
                    service.reset_cursor()
        except (WebSocketDisconnect, RuntimeError):
            stop.set()
        except Exception as exc:  # noqa: BLE001
            logger.debug("market ws receive ended: %s", exc)
            stop.set()

    receiver = asyncio.create_task(receive_loop())
    try:
        while not stop.is_set():
            try:
                frame = {
                    "type": "market.frame",
                    "sent_at": datetime.now(UTC).isoformat(),
                    "cursor": service.cursor.isoformat(),
                    "timeframe": timeframe,
                    "status": service.market_status().as_dict(),
                    "xray": service.xray(timeframe=timeframe),
                    "strength": service.strength(),
                    "leaders": service.leaders(timeframe=timeframe),
                }
            except Exception as exc:  # noqa: BLE001
                frame = {
                    "type": "market.error",
                    "sent_at": datetime.now(UTC).isoformat(),
                    "message": str(exc),
                }
            await websocket.send_json(frame)
            try:
                await asyncio.wait_for(stop.wait(), timeout=PUSH_INTERVAL_SECONDS)
            except TimeoutError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        stop.set()
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
