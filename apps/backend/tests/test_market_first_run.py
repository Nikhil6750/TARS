"""An installed TARS starts with nothing, and must say so usefully.

These run against a real empty directory and real (tiny) CSVs rather than
mocks: the failure being guarded against is a first launch that only shows an
error, which a mocked store cannot reproduce.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import market_xray
from market import setup
from market import state as market_state
from market.service import MarketXRayService
from market.store import CandleStore

HEADER = "Time (UTC),Open,High,Low,Close,Volume\n"


def _write_csv(directory: Path, symbol: str, side: str, minutes: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    lines = [HEADER]
    price = 1.1 if side == "Bid" else 1.1002
    for i in range(minutes):
        stamp = f"2026.01.05 {10 + i // 60:02d}:{i % 60:02d}:00"
        price += 0.00001 * ((i % 7) - 3)
        lines.append(f"{stamp},{price:.5f},{price + 0.0002:.5f},{price - 0.0002:.5f},{price + 0.00005:.5f},10\n")
    (directory / f"{symbol}_1 Min_{side}_2026.01.01_2026.01.05.csv").write_text(
        "".join(lines), encoding="utf-8"
    )


@pytest.fixture
def empty_client(tmp_path):
    """The real market routes over a store and database that do not exist yet.
    Just the routes: booting the whole app loads voice models for every test."""
    app = FastAPI()
    app.include_router(market_xray.router)
    app.state.market_xray_service = MarketXRayService(
        store=CandleStore(tmp_path / "store"), state=market_state.MarketState(tmp_path / "s.db")
    )
    with TestClient(app) as client:
        yield client, tmp_path


def test_an_empty_store_is_reported_not_crashed(empty_client):
    client, _ = empty_client
    body = client.get("/api/v1/market/setup", params={"csv_root": "Z:/nowhere"}).json()
    assert body["ready"] is False
    assert body["symbols_in_store"] == 0
    assert body["sources"]["csv_root_exists"] is False
    assert body["sources"]["instruments_found"] == []
    assert body["trading_execution"] == "DISABLED"


def test_an_empty_store_never_reads_as_live(empty_client):
    client, _ = empty_client
    status = client.get("/api/v1/market/setup").json()["status"]
    assert status["status"] != "LIVE"
    assert status["replay"] is False
    assert status["data_as_of"] is None


def test_discovery_needs_both_sides_of_an_instrument(tmp_path):
    forex = tmp_path / "Forex_UTC"
    _write_csv(forex, "EURUSD", "Bid", 5)
    _write_csv(forex, "EURUSD", "Ask", 5)
    _write_csv(forex, "GBPUSD", "Bid", 5)
    found = setup.discover_sources(tmp_path)
    assert found["instruments_found"] == ["EURUSD"]
    assert "GBPUSD" in found["instruments_missing"]


def test_an_import_from_a_folder_that_is_not_there_is_refused(empty_client):
    client, _ = empty_client
    response = client.post("/api/v1/market/import", params={"csv_root": "Z:/nowhere"})
    assert response.status_code == 400


def test_an_import_with_no_usable_files_fails_visibly(empty_client):
    client, tmp_path = empty_client
    empty = tmp_path / "no-csvs"
    empty.mkdir()
    client.post("/api/v1/market/import", params={"csv_root": str(empty)})
    for _ in range(100):
        state = client.get("/api/v1/market/setup").json()["import"]
        if state["state"] != "RUNNING":
            break
        time.sleep(0.05)
    assert state["state"] == "FAILED"
    assert "no bid/ask CSV" in state["error"]


def test_importing_real_files_makes_the_dashboard_ready(empty_client):
    client, tmp_path = empty_client
    csvs = tmp_path / "csvs"
    forex = csvs / "Forex_UTC"
    for side in ("Bid", "Ask"):
        _write_csv(forex, "EURUSD", side, 400)
    assert client.post("/api/v1/market/import", params={"csv_root": str(csvs)}).status_code == 200
    for _ in range(600):
        body = client.get("/api/v1/market/setup").json()
        if body["import"]["state"] != "RUNNING":
            break
        time.sleep(0.05)
    assert body["import"]["state"] == "DONE", body["import"]
    assert body["import"]["result"]["instruments_imported"] == ["EURUSD"]
    assert body["ready"] is True
    assert body["status"]["status"] == "REPLAY"


def test_only_the_store_root_is_written(tmp_path):
    """Importing must not create anything beside the store it was given."""
    csvs = tmp_path / "csvs"
    for side in ("Bid", "Ask"):
        _write_csv(csvs / "Forex_UTC", "EURUSD", side, 60)
    before = {p for p in csvs.rglob("*")}
    store = tmp_path / "store"
    job = setup.ImportJob()
    job.start(csv_root=csvs, store_root=store)
    job._thread.join(timeout=60)
    assert job.status()["state"] == "DONE"
    assert {p for p in csvs.rglob("*")} == before


# ---- writable state survives an install directory being replaced --------


def test_the_state_database_follows_tars_home(monkeypatch, tmp_path):
    monkeypatch.setenv("TARS_HOME", str(tmp_path))
    assert market_state.default_db() == tmp_path / "storage" / "market_xray.db"


def test_the_checkout_database_no_longer_depends_on_the_working_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("TARS_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    assert market_state.default_db().is_absolute()
    assert market_state.default_db().parent.name == "storage"
    assert tmp_path not in market_state.default_db().parents


def _db_with_event(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE history (value TEXT)")
    con.execute("INSERT INTO history VALUES ('kept')")
    con.commit()
    con.close()


def test_legacy_history_is_migrated_not_lost(tmp_path):
    legacy, target = tmp_path / "old" / "x.db", tmp_path / "new" / "x.db"
    _db_with_event(legacy)
    assert market_state.migrate_legacy_db(target, legacy) is True
    con = sqlite3.connect(target)
    assert con.execute("SELECT value FROM history").fetchall() == [("kept",)]
    con.close()
    assert legacy.exists(), "the original is the user's data and must be left alone"


def test_migration_never_overwrites_existing_history(tmp_path):
    legacy, target = tmp_path / "old.db", tmp_path / "new.db"
    _db_with_event(legacy)
    target.write_bytes(b"existing")
    assert market_state.migrate_legacy_db(target, legacy) is False
    assert target.read_bytes() == b"existing"


def test_migration_with_nothing_to_migrate_is_a_no_op(tmp_path):
    assert market_state.migrate_legacy_db(tmp_path / "a.db", tmp_path / "missing.db") is False
    assert not (tmp_path / "a.db").exists()
