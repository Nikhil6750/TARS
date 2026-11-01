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


def _write_generation(root: Path, revision: str, price: float) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    generation = root / "generations" / revision
    for tf, minutes in universe.TIMEFRAMES.items():
        folder = generation / "EURUSD"
        folder.mkdir(parents=True, exist_ok=True)
        t = datetime(2026, 6, 3, tzinfo=UTC)
        stamps = [t + timedelta(minutes=minutes * i) for i in range(180)]
        pq.write_table(pa.table({
            "ts": stamps,
            "open": [price] * len(stamps),
            "high": [price + 0.002] * len(stamps),
            "low": [price - 0.002] * len(stamps),
            "close": [price] * len(stamps),
            "spread": [0.0001] * len(stamps),
            "volume": [5.0] * len(stamps),
        }), folder / f"{tf}.parquet")


def test_external_generation_switch_replaces_service_without_restart(tmp_path):
    root = tmp_path / "store"
    _write_generation(root, "v1", 1.104)
    (root / "manifest.json").write_text(json.dumps({
        "dataset_revision": "v1", "generation": "generations/v1",
    }))
    state = MarketState(tmp_path / "market.db")
    previous = MarketXRayService(CandleStore(root), state=state)
    app = SimpleNamespace(state=SimpleNamespace(market_xray_service=previous))
    assert market_xray._app_service(app) is previous

    _write_generation(root, "v2", 1.5)
    pending = root / ".manifest-pending"
    pending.write_text(json.dumps({
        "dataset_revision": "v2", "generation": "generations/v2",
    }))
    pending.replace(root / "manifest.json")

    updated = market_xray._app_service(app)
    try:
        assert updated is not previous
        assert updated.store.recorded_revision == "v2"
        assert previous.store.recorded_revision == "v1"
        assert updated.store.path_for("EURUSD", "M5").exists()
    finally:
        updated.store.close()
        previous.store.close()


def test_frame_panels_all_observe_one_live_revision(tmp_path):
    """A mid-build feed write waits, rather than mixing X-Ray and leaders."""
    store = CandleStore(tmp_path / "store")
    store.put_live_batch([("EURUSD", "M5", [_bar(1.104)])])
    at = datetime(2026, 6, 3, 12, 5, tzinfo=UTC)
    begun = threading.Event()
    committed = threading.Event()
    worker = None

    class Panels:
        def __init__(self):
            self.store = store

        def market_status(self):
            return None

        def price(self):
            candle = store.latest_close("EURUSD", "M5", as_of=at)
            return candle.close

        def xray(self, **_):
            nonlocal worker
            def write():
                begun.set()
                store.put_live_batch([("EURUSD", "M5", [_bar(1.5)])])
                committed.set()
            worker = threading.Thread(target=write)
            worker.start()
            assert begun.wait(2)
            return {"price": self.price()}

        def strength(self, **_):
            assert not committed.wait(0.05), "writer committed within snapshot"
            return {"price": self.price()}

        def leaders(self, **_):
            return {"price": self.price()}

    try:
        service = Panels()
        inputs = FrameInputs(service, "M5", at, store.data_revision, False, True)
        frame = market_xray._build_frame(inputs, "M5")
        assert frame["xray"]["price"] == frame["strength"]["price"] == frame["leaders"]["price"]
        assert frame["xray"]["price"] == pytest.approx(1.104)
        assert frame["data_revision"] == inputs.data_revision
    finally:
        if worker is not None:
            worker.join(timeout=4)
        store.close()
    assert committed.is_set()


def test_targeted_import_retains_unmodified_symbols(tmp_path, monkeypatch):
    root = tmp_path / "store"

    def make_one(_con, symbol, *, store_root, start, csv_root):
        folder = store_root / symbol
        folder.mkdir(parents=True)
        path = folder / "M5.parquet"
        path.write_bytes(symbol.encode())
        return [ingest.IngestResult(symbol, "M5", 1, None, None, None, str(path))]

    monkeypatch.setattr(ingest, "ingest_symbol", make_one)
    initial = ingest.ingest_all(["EURUSD"], store_root=root)
    updated = ingest.ingest_all(["GBPUSD"], store_root=root)
    files = root / updated["generation"]
    assert (files / "EURUSD" / "M5.parquet").read_bytes() == b"EURUSD"
    assert (files / "GBPUSD" / "M5.parquet").read_bytes() == b"GBPUSD"
    assert initial["dataset_revision"] != updated["dataset_revision"]
    assert set(updated["symbols_ok"]) == {"EURUSD", "GBPUSD"}


@pytest.mark.asyncio
async def test_replay_step_during_build_discards_old_cursor(monkeypatch):
    start = datetime(2026, 6, 3, 12, tzinfo=UTC)
    building = threading.Event()
    release = threading.Event()

    class Stub:
        bounds = (start, start + timedelta(days=1))

        def __init__(self):
            self.position = 0
            self.store = SimpleNamespace(data_revision=0)

        def frame_inputs(self, timeframe):
            return FrameInputs(
                self, timeframe, start + timedelta(minutes=self.position),
                0, True, False,
            )

        def frame_labels(self, _inputs):
            return {}

        def step_cursor(self, *, timeframe, bars):
            self.position += bars
            release.set()
            return start + timedelta(minutes=self.position)

    class Socket:
        app = SimpleNamespace(state=SimpleNamespace())

        def __init__(self):
            self.sent = []
            self.delivered = asyncio.Event()
            self.first = True

        async def accept(self):
            pass

        async def receive_json(self):
            if self.first:
                self.first = False
                assert await asyncio.to_thread(building.wait, 5)
                return {"type": "replay.step", "bars": 1}
            await self.delivered.wait()
            raise WebSocketDisconnect(code=1000)

        async def send_json(self, frame):
            self.sent.append(frame)
            self.delivered.set()

    stub = Stub()
    monkeypatch.setattr(market_xray, "_app_service", lambda _app: stub)

    def build(inputs, timeframe):
        if inputs.as_of == start:
            building.set()
            assert release.wait(5)
        return {
            "type": "market.frame",
            "timeframe": timeframe,
            "cursor": inputs.as_of.isoformat(),
        }

    monkeypatch.setattr(market_xray, "_build_frame", build)
    frames = SharedFrames(retained=2)
    monkeypatch.setattr(market_xray, "_frames", frames)
    sock = Socket()
    try:
        await asyncio.wait_for(market_xray.market_ws(sock), timeout=8)
        assert sock.sent
        assert sock.sent[0]["cursor"] == (start + timedelta(minutes=1)).isoformat()
        assert not any(frame["cursor"] == start.isoformat() for frame in sock.sent)
    finally:
        frames._executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.asyncio
async def test_concurrent_subscribers_get_their_own_initial_timeframes(monkeypatch):
    class Stub:
        bounds = (None, None)

        def frame_inputs(self, tf):
            return FrameInputs(self, tf, None, 0, False, False)

        def frame_labels(self, _inputs):
            return {}

    class Socket:
        app = SimpleNamespace(state=SimpleNamespace())

        def __init__(self, tf):
            self.tf = tf
            self.first = True
            self.delivered = asyncio.Event()
            self.sent = []

        async def accept(self):
            pass

        async def receive_json(self):
            if self.first:
                self.first = False
                return {"type": "subscribe", "timeframe": self.tf}
            await self.delivered.wait()
            raise WebSocketDisconnect(code=1000)

        async def send_json(self, frame):
            self.sent.append(frame)
            self.delivered.set()

    stub = Stub()
    monkeypatch.setattr(market_xray, "_app_service", lambda _app: stub)
    monkeypatch.setattr(
        market_xray, "_build_frame",
        lambda inputs, tf: {"type": "market.frame", "timeframe": tf},
    )
    frames = SharedFrames(retained=4)
    monkeypatch.setattr(market_xray, "_frames", frames)
    clients = [Socket("M5"), Socket("H4")]
    try:
        await asyncio.wait_for(
            asyncio.gather(*(market_xray.market_ws(client) for client in clients)),
            timeout=6,
        )
        assert [client.sent[0]["timeframe"] for client in clients] == ["M5", "H4"]
    finally:
        frames._executor.shutdown(wait=True, cancel_futures=True)
