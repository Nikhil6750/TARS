from __future__ import annotations

import asyncio
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.routers import market_xray
from market import universe
from market.frames import SharedFrames
from market.service import MarketXRayService
from market.state import MarketState
from market.store import CandleStore


@pytest.mark.asyncio
async def test_market_websocket_does_not_send_after_receiver_disconnect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pin the disconnect/build race without requiring a live Uvicorn server.

    The frame build deliberately outlives the receiver.  A correct sender
    observes the shared stop event after the worker returns and exits without
    attempting another websocket.send.
    """

    import pyarrow as pa
    import pyarrow.parquet as pq

    # Isolated test-only history, never the user's recorded store. Exercise
    # the actual panel builder, including DuckDB, rather than an error frame.
    root = tmp_path / "candles" / "EURUSD"
    root.mkdir(parents=True)
    end = datetime(2026, 6, 3, 12, tzinfo=UTC)
    for tf, minutes in universe.TIMEFRAMES.items():
        pq.write_table(pa.table({
            "ts": [end - timedelta(minutes=minutes * i) for i in range(180, 0, -1)],
            "open": [1.1] * 180, "high": [1.12] * 180,
            "low": [1.08] * 180, "close": [1.1 + (i % 7) * 0.001 for i in range(180)],
            "spread": [0.0001] * 180, "volume": [1.0] * 180,
        }), root / f"{tf}.parquet")
    service = MarketXRayService(CandleStore(root.parent), MarketState(tmp_path / "state.db"))
    started, disconnected, finished = threading.Event(), threading.Event(), threading.Event()
    built = []

    class DisconnectedSocket:
        app = object()

        def __init__(self) -> None:
            self.disconnected = False
            self.sends = 0

        async def accept(self) -> None:
            pass

        async def receive_json(self) -> dict:
            assert await asyncio.to_thread(started.wait, 5), "frame build never started"
            self.disconnected = True
            disconnected.set()
            raise WebSocketDisconnect(code=1000)

        async def send_json(self, _frame: dict) -> None:
            self.sends += 1
            if self.disconnected:
                raise RuntimeError(
                    "Unexpected ASGI message 'websocket.send', after sending "
                    "'websocket.close' or response already completed."
                )

    monkeypatch.setattr(market_xray, "_app_service", lambda _app: service)
    shared = SharedFrames(retained=2)
    monkeypatch.setattr(market_xray, "_frames", shared)
    real_build = market_xray._build_frame

    def slow_frame(_service: object, _timeframe: str) -> dict:
        started.set()
        assert disconnected.wait(5), "receiver never disconnected"
        result = real_build(_service, _timeframe)
        built.append(result)
        finished.set()
        return result

    monkeypatch.setattr(market_xray, "_build_frame", slow_frame)
    socket = DisconnectedSocket()
    try:
        await asyncio.wait_for(market_xray.market_ws(socket), 15)  # type: ignore[arg-type]
        assert finished.is_set()
        assert len(built) == 1 and built[0]["type"] == "market.frame"
        assert built[0]["xray"]["series"], "real candles must reach the panel"
        assert socket.sends == 0
        assert shared.counts()["flights"] == 0
    finally:
        shared._executor.shutdown(wait=True, cancel_futures=True)
        service.store.close()
