from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.routers import market_xray


@pytest.mark.asyncio
@pytest.mark.xfail(
    reason="confirmed at 7fe5875: sender resumes after disconnect and sends on a closed ASGI socket",
    strict=False,
)
async def test_market_websocket_does_not_send_after_receiver_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the disconnect/build race without requiring a live Uvicorn server.

    The frame build deliberately outlives the receiver.  A correct sender
    observes the shared stop event after the worker returns and exits without
    attempting another websocket.send.
    """

    class DisconnectedSocket:
        app = object()

        def __init__(self) -> None:
            self.disconnected = False

        async def accept(self) -> None:
            pass

        async def receive_json(self) -> dict:
            self.disconnected = True
            raise WebSocketDisconnect(code=1000)

        async def send_json(self, _frame: dict) -> None:
            if self.disconnected:
                raise RuntimeError(
                    "Unexpected ASGI message 'websocket.send', after sending "
                    "'websocket.close' or response already completed."
                )

    monkeypatch.setattr(market_xray, "_app_service", lambda _app: object())

    def slow_frame(_service: object, _timeframe: str) -> dict:
        time.sleep(0.05)
        return {"type": "market.frame"}

    monkeypatch.setattr(market_xray, "_build_frame", slow_frame)
    await market_xray.market_ws(DisconnectedSocket())  # type: ignore[arg-type]
