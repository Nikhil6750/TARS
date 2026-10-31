"""First-run setup: what the dashboard needs, and importing it from the app.

An installed TARS starts with an empty candle store, and "run this command in
a terminal" is not an answer on a machine that has no checkout. So the backend
reports what it found and can run the import itself. It never invents data:
with no source files the dashboard stays empty and says what is missing.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from market import ingest, universe

logger = logging.getLogger("tars.market.setup")


def discover_sources(csv_root: Path) -> dict:
    """Which universe instruments have both bid and ask files under a root."""
    found: list[str] = []
    missing: list[str] = []
    for symbol, meta in universe.INSTRUMENTS.items():
        directory = csv_root / meta.source_dir
        sides = [
            any(directory.glob(meta.source_stem + "_" + side + "_*.csv"))
            for side in ("Bid", "Ask")
        ] if directory.is_dir() else [False, False]
        (found if all(sides) else missing).append(symbol)
    return {
        "csv_root": str(csv_root),
        "csv_root_exists": csv_root.is_dir(),
        "instruments_found": sorted(found),
        "instruments_missing": sorted(missing),
    }


class ImportJob:
    """One background import at a time. Failures are reported, never raised:
    this runs behind a button, not a terminal."""

    def __init__(self, on_complete: Callable[[], None] | None = None):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._on_complete = on_complete
        self._state: dict = {"state": "IDLE", "error": None, "result": None}

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def start(self, *, csv_root: Path, store_root: Path) -> dict:
        with self._lock:
            if self._state["state"] == "RUNNING":
                return dict(self._state)
            self._state = {
                "state": "RUNNING", "error": None, "result": None,
                "started_at": datetime.now(UTC).isoformat(), "csv_root": str(csv_root),
            }
            self._thread = threading.Thread(
                target=self._run, args=(csv_root, store_root), name="market-import", daemon=True
            )
            self._thread.start()
            return dict(self._state)

    def _run(self, csv_root: Path, store_root: Path) -> None:
        try:
            found = discover_sources(csv_root)["instruments_found"]
            if not found:
                raise FileNotFoundError(f"no bid/ask CSV pairs found under {csv_root}")
            manifest = ingest.ingest_all(found, store_root=store_root, csv_root=csv_root)
            outcome = {
                "state": "DONE", "error": None,
                "result": {
                    "instruments_imported": sorted(manifest["symbols_ok"]),
                    "instruments_failed": manifest["symbols_failed"],
                    "elapsed_seconds": manifest["elapsed_seconds"],
                },
            }
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            logger.warning("market import failed: %s", exc)
            outcome = {"state": "FAILED", "error": f"{type(exc).__name__}: {exc}", "result": None}
        if self._on_complete and outcome["state"] == "DONE":
            try:
                self._on_complete()
            except Exception as exc:  # noqa: BLE001
                logger.warning("market reload after import failed: %s", exc)
        with self._lock:
            self._state = {**self._state, **outcome}
