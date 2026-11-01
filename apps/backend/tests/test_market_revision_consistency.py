"""Atomic recorded generations and coherent live WebSocket frame revisions."""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketDisconnect

from app.routers import market_xray
from market import ingest, universe
from market.frames import SharedFrames
from market.service import FrameInputs, MarketXRayService
from market.state import MarketState
from market.store import (
    Candle, CandleStore, RevisionChanged, active_dataset_revision,
)


def _bar(price: float, minute: int = 0) -> Candle:
    return Candle(
        datetime(2026, 6, 3, 12, minute, tzinfo=UTC),
        price, price + 0.002, price - 0.002, price,
        0.0001, 5.0,
    )


def test_staged_import_changes_revision_only_on_complete_publish(tmp_path, monkeypatch):
    root = tmp_path / "store"
    made = []

    def fake_ingest(_con, symbol, *, store_root, start, csv_root):
        folder = store_root / symbol
        folder.mkdir(parents=True)
        target = folder / "M5.parquet"
        target.write_bytes(b"test-only-dataset")
        made.append(target)
        return [
            ingest.IngestResult(symbol, "M5", 1, None, None, None, str(target))
        ]

    monkeypatch.setattr(ingest, "ingest_symbol", fake_ingest)
    first = ingest.ingest_all(["EURUSD"], store_root=root)
    initial_revision = first["dataset_revision"]
    assert active_dataset_revision(root) == initial_revision
    assert (root / first["generation"] / "EURUSD" / "M5.parquet").exists()

    second = ingest.ingest_all(["EURUSD"], store_root=root)
    assert active_dataset_revision(root) == second["dataset_revision"]
    assert second["dataset_revision"] != initial_revision
    assert made[0].exists(), "old generation must survive an in-flight reader"

    def fail(*_args, **_kwargs):
        raise RuntimeError("import interrupted")

    monkeypatch.setattr(ingest, "ingest_symbol", fail)
    with pytest.raises(ValueError, match="no usable candle"):
        ingest.ingest_all(["EURUSD"], store_root=root)
    assert active_dataset_revision(root) == second["dataset_revision"]


def test_store_pins_recorded_generation_across_manifest_changes(tmp_path):
    root = tmp_path / "store"
    for revision in ("a", "b"):
        folder = root / "generations" / revision / "EURUSD"
        folder.mkdir(parents=True)
        (folder / "M5.parquet").write_bytes(revision.encode())
    (root / "manifest.json").write_text(
        json.dumps({"dataset_revision": "a", "generation": "generations/a"})
    )
    old = CandleStore(root)
    try:
        assert old.path_for("EURUSD", "M5").read_bytes() == b"a"
        (root / "manifest.json").write_text(
            json.dumps({"dataset_revision": "b", "generation": "generations/b"})
        )
        assert active_dataset_revision(root) == "b"
        assert old.path_for("EURUSD", "M5").read_bytes() == b"a"
        newer = CandleStore(root)
        try:
            assert newer.path_for("EURUSD", "M5").read_bytes() == b"b"
        finally:
            newer.close()
    finally:
        old.close()


def test_live_snapshot_blocks_writer_until_all_panels_finish(tmp_path):
    store = CandleStore(tmp_path / "store")
    entered = threading.Event()
    completed = threading.Event()

    def writer():
        entered.set()
        store.put_live_batch([
            ("EURUSD", "M5", [_bar(1.104)]),
            ("GBPUSD", "M5", [_bar(1.5)]),
        ])
        completed.set()

    try:
        with store.consistent_snapshot(0):
            worker = threading.Thread(target=writer)
            worker.start()
            assert entered.wait(2)
            assert not completed.wait(0.05)
            assert store.data_revision == 0
        worker.join(timeout=3)
        assert completed.is_set()
        assert store.data_revision == 1
        with pytest.raises(RevisionChanged):
            with store.consistent_snapshot(0):
                pass
    finally:
        store.close()


def test_failed_live_batch_does_not_publish_partial_revision(tmp_path):
    store = CandleStore(tmp_path / "store")
    try:
        with pytest.raises(AttributeError):
            store.put_live_batch([
                ("EURUSD", "M5", [_bar(1.104)]),
                ("GBPUSD", "M5", [object()]),
            ])
        assert store.data_revision == 0
        assert store._query("SELECT count(*) FROM live_bars") == [(0,)]
        store.put_live_batch([("EURUSD", "M5", [_bar(1.104)])])
        assert store.data_revision == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_websocket_first_frame_uses_immediate_subscription(monkeypatch):
    """The on-open M5 subscription must win over default H1."""
    class StubService:
        bounds = (None, None)

        def frame_inputs(self, timeframe):
            return FrameInputs(self, timeframe, None, 0, False, False)

        def frame_labels(self, _inputs):
            return {}

    class Socket:
        app = SimpleNamespace(state=SimpleNamespace())

        def __init__(self):
            self.sent = []
            self.first = asyncio.Event()

        async def accept(self):
            pass

        async def receive_json(self):
            if not hasattr(self, "subscribed"):
                self.subscribed = True
                return {"type": "subscribe", "timeframe": "M5"}
            await self.first.wait()
            raise WebSocketDisconnect(code=1000)

        async def send_json(self, frame):
            self.sent.append(frame)
            self.first.set()

    test_service = StubService()
    monkeypatch.setattr(market_xray, "_app_service", lambda _app: test_service)
    monkeypatch.setattr(
        market_xray, "_build_frame",
        lambda inputs, timeframe: {"type": "market.frame", "timeframe": timeframe},
    )
    frames = SharedFrames(retained=2)
    monkeypatch.setattr(market_xray, "_frames", frames)
    socket = Socket()
    try:
        await asyncio.wait_for(market_xray.market_ws(socket), timeout=5)
        assert socket.sent
        assert socket.sent[0]["timeframe"] == "M5"
    finally:
        frames._executor.shutdown(wait=True, cancel_futures=True)
