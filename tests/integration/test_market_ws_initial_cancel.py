from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2] / "apps" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.routers import market_xray


@pytest.mark.asyncio
async def test_cancellation_during_initial_subscription_wait_cleans_receiver(monkeypatch):
    receiving = asyncio.Event()

    class Socket:
        app = object()

        async def accept(self):
            pass

        async def receive_json(self):
            receiving.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(market_xray, "_app_service", lambda _app: object())
    before = asyncio.all_tasks()
    handler = asyncio.create_task(market_xray.market_ws(Socket()))
    await asyncio.wait_for(receiving.wait(), 3)
    handler.cancel()
    await asyncio.gather(handler, return_exceptions=True)
    await asyncio.sleep(0)
    leaked = asyncio.all_tasks() - before
    # Clean even when exposing the defect, so it cannot contaminate other tests.
    for task in leaked:
        task.cancel()
    await asyncio.gather(*leaked, return_exceptions=True)
    assert not leaked, f"receiver survives cancellation in initial handshake: {leaked}"
