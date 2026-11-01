"""Independent immutable-publication checks using isolated, real CSV imports."""
from __future__ import annotations

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2] / "apps" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from market import ingest
from market.store import CandleStore


def sources(root, symbols):
    folder = root / "Forex_UTC"
    folder.mkdir(parents=True, exist_ok=True)
    for symbol in symbols:
        for side, spread in (("Bid", 0), ("Ask", .0002)):
            rows = ["Time (UTC),Open,High,Low,Close,Volume"]
            for i in range(180):
                price = 1.1 + spread + i * .00001
                at = (datetime(2026, 1, 5, tzinfo=UTC) + timedelta(minutes=i)).strftime("%Y.%m.%d %H:%M:%S")
                rows.append(f"{at},{price},{price+.001},{price-.001},{price},1")
            (folder / f"{symbol}_1 Min_{side}_2026.01.01_2026.01.12.csv").write_text(
                "\n".join(rows) + "\n", encoding="utf-8"
            )


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="confirmed: failed replacement drops active symbol")
def test_failed_target_keeps_previously_available_symbol(tmp_path):
    csvs, root = tmp_path / "csv", tmp_path / "store"
    sources(csvs, ["EURUSD", "GBPUSD"])
    ingest.ingest_all(["EURUSD", "GBPUSD"], store_root=root, csv_root=csvs)
    # Simulate missing/unreadable replacement source, confined to test fixtures.
    for path in (csvs / "Forex_UTC").glob("GBPUSD*.csv"):
        path.unlink()
    result = ingest.ingest_all(["EURUSD", "GBPUSD"], store_root=root, csv_root=csvs)
    assert "GBPUSD" in result["symbols_failed"]
    store = CandleStore(root)
    try:
        assert "GBPUSD" in store.recorded_symbols(), (
            "partial import removed previously usable GBPUSD when its replacement failed"
        )
    finally:
        store.close()


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="confirmed: concurrent imports lose a committed target")
def test_concurrent_targeted_imports_preserve_both_commits(tmp_path, monkeypatch):
    csvs, root = tmp_path / "csv", tmp_path / "store"
    sources(csvs, ["EURUSD", "GBPUSD", "AUDUSD"])
    ingest.ingest_all(["EURUSD"], store_root=root, csv_root=csvs)
    barrier = threading.Barrier(2)
    publish = threading.Lock()
    real_replace = os.replace

    def synchronized_publish(source, destination):
        if Path(destination) == root / "manifest.json":
            barrier.wait(timeout=15)
        # Serialize only the final OS rename, after both generations have
        # read their base. This avoids Windows simultaneous-handle noise and
        # exposes whether successful publications preserve both updates.
        with publish:
            return real_replace(source, destination)

    monkeypatch.setattr(ingest.os, "replace", synchronized_publish)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(ingest.ingest_all, [symbol], store_root=root, csv_root=csvs)
                   for symbol in ("GBPUSD", "AUDUSD")]
        results = [future.result(timeout=30) for future in futures]
    assert all(not result["symbols_failed"] for result in results)
    current = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert {"EURUSD", "GBPUSD", "AUDUSD"} <= set(current["symbols_ok"]), (
        f"both imports succeeded but final publication lost one update: {current['symbols_ok']}"
    )


def test_immutable_generations_retain_old_files_and_reuse_untouched_data(tmp_path):
    csvs, root = tmp_path / "csv", tmp_path / "store"
    sources(csvs, ["EURUSD", "GBPUSD"])
    first = ingest.ingest_all(["EURUSD", "GBPUSD"], store_root=root, csv_root=csvs)
    old = root / first["generation"]
    second = ingest.ingest_all(["EURUSD"], store_root=root, csv_root=csvs)
    new = root / second["generation"]
    old_pair = old / "GBPUSD" / "M5.parquet"
    new_pair = new / "GBPUSD" / "M5.parquet"
    assert old_pair.read_bytes() == new_pair.read_bytes()
    assert (old / "EURUSD" / "M5.parquet").exists()
    files = list(root.rglob("*.parquet"))
    print({"generations": len(list((root / "generations").iterdir())),
           "logical_parquet_bytes": sum(p.stat().st_size for p in files),
           "unchanged_pair_hardlinked": old_pair.samefile(new_pair),
           "updated_pair_hardlinked": (old / "EURUSD" / "M5.parquet").samefile(
               new / "EURUSD" / "M5.parquet")})
