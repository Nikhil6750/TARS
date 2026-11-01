"""Independent external-writer cache probe; all generated history is test-only."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[2] / "apps" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.routers import market_xray
from market.frames import SharedFrames
from market.service import MarketXRayService
from market.state import MarketState
from market.store import CandleStore


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="confirmed: standalone import does not invalidate shared replay frames")
def test_standalone_import_invalidates_retained_replay(tmp_path, monkeypatch):
    csvs, root = tmp_path / "csv", tmp_path / "store"
    csvs.mkdir()
    (csvs / "Forex_UTC").mkdir()

    def write_sources(correction):
        for side, spread in (("Bid", 0), ("Ask", 0.0002)):
            rows = ["Time (UTC),Open,High,Low,Close,Volume"]
            start = datetime(2026, 1, 5, tzinfo=UTC)
            for i in range(10_000):
                price = 1.1 + (i % 71) * 0.00001 + spread
                if i > 9800:
                    price += correction
                stamp = (start + timedelta(minutes=i)).strftime("%Y.%m.%d %H:%M:%S")
                rows.append(f"{stamp},{price},{price+.001},{price-.001},{price},1")
            (csvs / "Forex_UTC" / f"EURUSD_1 Min_{side}_2026.01.01_2026.01.12.csv").write_text(
                "\n".join(rows) + "\n", encoding="utf-8"
            )

    def standalone_import():
        result = subprocess.run(
            [sys.executable, "-m", "market.ingest", "--symbols", "EURUSD",
             "--store-root", str(root)], cwd=BACKEND,
            env={**os.environ, "TARS_MARKET_CSV_ROOT": str(csvs)},
            capture_output=True, text=True, timeout=60, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "ok=1 failed=0" in result.stdout, result.stdout + result.stderr

    write_sources(0)
    standalone_import()
    service = MarketXRayService(CandleStore(root), MarketState(tmp_path / "state.db"))
    shared = SharedFrames(retained=2)
    monkeypatch.setattr(market_xray, "_frames", shared)
    app = FastAPI()
    app.include_router(market_xray.router)
    app.state.market_xray_service = service
    try:
        with TestClient(app) as client:
            before_key = service.frame_inputs("H1")
            with client.websocket_connect("/api/v1/market/ws") as ws:
                before = ws.receive_json()
            write_sources(0.1)
            standalone_import()
            after_key = service.frame_inputs("H1")
            fresh = client.get("/api/v1/market/xray?timeframe=H1").json()
            assert fresh["series"] != before["xray"]["series"], "import must change actual bars"
            with client.websocket_connect("/api/v1/market/ws") as ws:
                replay = ws.receive_json()
            assert replay["xray"]["series"] == fresh["series"], (
                "standalone import changed candles but retained replay is obsolete; "
                f"key unchanged={before_key == after_key}"
            )
    finally:
        shared._executor.shutdown(wait=True, cancel_futures=True)
        service.store.close()
