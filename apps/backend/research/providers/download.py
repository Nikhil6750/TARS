"""Polite, resumable download machinery for large public-datafeed pulls.

* RateGovernor  - AIMD concurrency: halves on HTTP 429/503 (and honours Retry-After), grows back slowly after sustained
                  success; also enforces a minimum spacing between request starts so the provider is never hammered.
* Manifest      - SQLite record of every file ever fetched (url, sha256, size, rows, attempts, timestamps). Verified files are
                  skipped on resume; nothing is downloaded twice.
* verify_bi5    - integrity check of a Dukascopy candle file (LZMA ok, 24-byte records, monotone in-day seconds)."""
from __future__ import annotations

import lzma
import random
import sqlite3
import struct
import threading
import time
from pathlib import Path

RECORD_SIZE = 24


class HttpFailure(RuntimeError):
    def __init__(self, status: int, retry_after: float | None = None, msg: str = "") -> None:
        super().__init__(f"HTTP {status} {msg}".strip())
        self.status, self.retry_after = status, retry_after


class IntegrityError(RuntimeError):
    pass


def verify_bi5(payload: bytes) -> int:
    """Return the number of candle rows (0 for an empty day). Raises IntegrityError on a corrupt/truncated file."""
    if not payload:
        return 0
    try:
        body = lzma.decompress(payload)
    except lzma.LZMAError as exc:
        raise IntegrityError(f"not valid LZMA: {exc}") from exc
    if len(body) % RECORD_SIZE:
        raise IntegrityError(f"decompressed size {len(body)} is not a multiple of {RECORD_SIZE}")
    n = len(body) // RECORD_SIZE
    if n > 1440:
        raise IntegrityError(f"{n} rows exceeds one day of M1")
    last = -1
    for i in range(n):
        sec = struct.unpack_from(">I", body, i * RECORD_SIZE)[0]
        if sec <= last or sec >= 86400:
            raise IntegrityError(f"row {i}: seconds offset {sec} not strictly increasing within the day")
        last = sec
    return n


class RateGovernor:
    def __init__(self, max_workers: int = 4, min_interval: float = 0.25, clock=time.monotonic, sleep=time.sleep, floor: int = 2) -> None:
        self.max_workers, self.limit = max_workers, max_workers
        self.floor = min(floor, max_workers)
        self.min_interval = min_interval
        self._active, self._pause_until, self._last_start, self._ok_streak = 0, 0.0, 0.0, 0
        self._cond = threading.Condition()
        self._clock, self._sleep = clock, sleep
        self.stats = {"requests": 0, "ok": 0, "throttled": 0, "errors": 0, "min_limit_seen": max_workers}

    def acquire(self) -> None:
        with self._cond:
            while True:
                now = self._clock()
                if self._active < self.limit and now >= self._pause_until:
                    wait = self._last_start + self.min_interval - now
                    if wait <= 0:
                        break
                    self._cond.wait(timeout=wait)
                    continue
                self._cond.wait(timeout=max(self._pause_until - now, 0.05) if now < self._pause_until else 0.1)
            self._active += 1
            self._last_start = self._clock()
            self.stats["requests"] += 1

    def release(self, outcome: str, retry_after: float | None = None) -> None:
        """outcome: 'ok' | 'miss' (503 = CDN edge cache miss: the file is being fetched upstream, retry the SAME file later - no
        concurrency change) | 'throttled' (429 or explicit Retry-After: back off globally) | 'error' (network fault)."""
        with self._cond:
            self._active -= 1
            if outcome == "throttled":
                self.stats["throttled"] += 1
                self.limit = max(self.floor, self.limit - 1)   # additive decrease: 503s are routine on this CDN, not an emergency
                self.stats["min_limit_seen"] = min(self.stats["min_limit_seen"], self.limit)
                self._pause_until = max(self._pause_until, self._clock() + (retry_after if retry_after else 1.0 + random.random() * 2.0))
                self._ok_streak = 0
            elif outcome == "miss":
                self.stats["misses"] = self.stats.get("misses", 0) + 1
                self._ok_streak = 0
            elif outcome == "ok":
                self.stats["ok"] += 1
                self._ok_streak += 1
                if self._ok_streak >= 8 and self.limit < self.max_workers:
                    self.limit += 1
                    self._ok_streak = 0
            else:
                self.stats["errors"] += 1
                self._ok_streak = 0
            self._cond.notify_all()


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 30.0, rng=random.random) -> float:
    """Exponential backoff with full-range jitter: base*2^n capped, scaled by a factor in [0.5, 1.5)."""
    return min(cap, base * (2 ** attempt)) * (0.5 + rng())


class Manifest:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS files (
                key TEXT PRIMARY KEY, url TEXT NOT NULL, rel_path TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
                rows INTEGER NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL, retrieved_at TEXT NOT NULL)""")

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=60)
        c.row_factory = sqlite3.Row
        return c

    def get(self, key: str):
        with self._conn() as c:
            r = c.execute("SELECT * FROM files WHERE key=?", (key,)).fetchone()
        return dict(r) if r else None

    def put(self, key: str, url: str, rel_path: str, sha256: str, size: int, rows: int, status: str, attempts: int, retrieved_at: str) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?)",
                      (key, url, rel_path, sha256, size, rows, status, attempts, retrieved_at))

    def count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT count(*) FROM files").fetchone()[0]

    def summary(self) -> dict:
        with self._conn() as c:
            rows = c.execute("SELECT status, count(*) n, sum(size) b, sum(attempts) a FROM files GROUP BY status").fetchall()
        return {r["status"]: {"files": r["n"], "bytes": r["b"], "attempts": r["a"]} for r in rows}
