"""Read-only DuckDB access to the Market X-Ray candle store.

Every read is bounded by an explicit ``as_of`` timestamp and only ever returns
bars whose close time is at or before it. That single rule is what keeps the
whole dashboard free of look-ahead: replaying to an earlier moment cannot
reveal a bar that had not completed yet, because the filter is applied in SQL
rather than trimmed afterwards.

A bar's ``ts`` is its *open*. Comparing that directly with ``as_of`` would
return the bar still forming at that moment, complete with prices from after
it, so every read compares against `_completed_before`, which subtracts the
bar's own length.
"""
from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import duckdb
import pyarrow as pa

from market import universe
from market.ingest import DEFAULT_STORE_ROOT  # resolved from TARS_DATA_ROOT; see P1-C2

logger = logging.getLogger("tars.market.store")


class RevisionChanged(RuntimeError):
    """A frame began under a live-data revision that has since changed."""


def active_dataset_revision(root: Path | str) -> str:
    """Cross-process revision published atomically with the active generation.

    Legacy stores have no revision marker. A malformed manifest is not
    mistaken for the old generation: callers must fail closed.
    """
    path = Path(root) / "manifest.json"
    if not path.exists():
        return "legacy"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    return str(manifest.get("dataset_revision") or "legacy")



@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    spread: float
    volume: float

    def as_dict(self) -> dict:
        return {
            "ts": self.ts.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "spread": self.spread,
            "volume": self.volume,
        }


@dataclass(frozen=True)
class Coverage:
    symbol: str
    timeframe: str
    bars: int
    first_bar: datetime | None
    last_bar: datetime | None


class CandleStore:
    """Thread-safe reader over the Parquet candle store.

    DuckDB connections are not safe to share across threads, so one shared
    connection is guarded by a lock. Reads are short analytical scans over
    column-pruned Parquet, so a single serialized reader is ample for a
    local single-user dashboard.
    """

    def __init__(self, root: Path | str = DEFAULT_STORE_ROOT):
        self.root = Path(root)
        self._lock = threading.RLock()
        self._con = duckdb.connect(database=":memory:")
        self._con.execute("SET TimeZone='UTC'")
        # Bars from a live feed. Held in memory, apart from the recorded
        # Parquet, because the feed can always resupply them and the recorded
        # store must stay a pure record of what was imported.
        self._con.execute(
            "CREATE TABLE live_bars (symbol VARCHAR, timeframe VARCHAR, ts TIMESTAMP, open DOUBLE,"
            " high DOUBLE, low DOUBLE, close DOUBLE, spread DOUBLE, volume DOUBLE,"
            " PRIMARY KEY (symbol, timeframe, ts))"
        )
        # Counts writes to `live_bars`, so results read from the store can be
        # told apart from ones read before a write. Recorded Parquet has a
        # separate immutable generation/revision published by the importer.
        self.data_revision = 0
        self.manifest = self._load_manifest()
        self.recorded_revision = str(self.manifest.get("dataset_revision") or "legacy")
        generation = self.manifest.get("generation")
        # A new generation is immutable and survives replacement of the
        # active manifest; readers holding this store never see partial imports.
        self._recorded_root = self.root / generation if generation else self.root

    def _load_manifest(self) -> dict:
        path = self.root / "manifest.json"
        if not path.exists():
            logger.warning("no candle-store manifest at %s", path)
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("unreadable manifest at %s: %s", path, exc)
            return {}

    # ---- paths -----------------------------------------------------------
    def path_for(self, symbol: str, timeframe: str) -> Path:
        return self._recorded_root / symbol.upper() / (timeframe.upper() + ".parquet")

    def has(self, symbol: str, timeframe: str) -> bool:
        return self.path_for(symbol, timeframe).exists() or self._has_live(symbol, timeframe)

    def _has_live(self, symbol: str, timeframe: str) -> bool:
        rows = self._query(
            "SELECT 1 FROM live_bars WHERE symbol = ? AND timeframe = ? LIMIT 1",
            [symbol.upper(), timeframe.upper()],
        )
        return bool(rows)

    @contextmanager
    def consistent_snapshot(self, expected_live_revision: int):
        """Hold one in-memory DuckDB revision across every panel read.

        A feed writer cannot modify one symbol halfway through a frame.
        The revision check happens *inside* the lock so a build queued behind
        a feed commit does not accidentally claim an earlier version.
        """
        with self._lock:
            if self.data_revision != expected_live_revision:
                raise RevisionChanged("live data changed before frame build")
            yield

    def put_live_bars(self, symbol: str, timeframe: str, bars: list[Candle]) -> None:
        """Compatibility seam for one symbol/timeframe, committed atomically."""
        self.put_live_batch([(symbol, timeframe, bars)])

    def put_live_batch(self, updates: list[tuple[str, str, list[Candle]]]) -> None:
        """Commit a whole feed poll in one DuckDB transaction/revision.

        Publishing per-symbol writes allowed X-Ray and leaders to observe
        different stages of the same poll. A failure rolls everything back
        and does not advance the revision.
        """
        updates = [(symbol, timeframe, bars) for symbol, timeframe, bars in updates if bars]
        if not updates:
            return
        with self._lock:
            self._con.execute("BEGIN TRANSACTION")
            try:
                for symbol, timeframe, bars in updates:
                    incoming = pa.table({
                        "symbol": [symbol.upper()] * len(bars),
                        "timeframe": [timeframe.upper()] * len(bars),
                        "ts": [_naive(b.ts) for b in bars],
                        "open": [b.open for b in bars],
                        "high": [b.high for b in bars],
                        "low": [b.low for b in bars],
                        "close": [b.close for b in bars],
                        "spread": [b.spread for b in bars],
                        "volume": [b.volume for b in bars],
                    })
                    self._con.register("incoming_bars", incoming)
                    try:
                        self._con.execute(
                            "INSERT OR REPLACE INTO live_bars SELECT * FROM incoming_bars"
                        )
                    finally:
                        self._con.unregister("incoming_bars")
                self._con.execute("COMMIT")
            except BaseException:
                self._con.execute("ROLLBACK")
                raise
            self.data_revision += 1

    @property
    def available_symbols(self) -> tuple[str, ...]:
        """Symbols in the data-backed universe that actually have files on
        disk for every advertised timeframe."""
        return tuple(
            s for s in universe.SYMBOLS
            if all(self.has(s, tf) for tf in universe.TIMEFRAMES)
        )

    def _scan(self, symbol: str, timeframe: str) -> str:
        """Recorded bars, then any live bars after the recorded ones end. An
        `as_of` inside recorded history therefore reads recorded data only,
        which is what keeps REPLAY a replay of what was recorded."""
        path = self.path_for(symbol, timeframe)
        key = "symbol = '" + symbol.upper() + "' AND timeframe = '" + timeframe.upper() + "'"
        columns = "ts, open, high, low, close, spread, volume"
        live = "SELECT " + columns + " FROM live_bars WHERE " + key
        if not path.exists():
            if not self._has_live(symbol, timeframe):
                raise FileNotFoundError("no candles for " + symbol + " " + timeframe)
            return "(" + live + ")"
        recorded = "read_parquet('" + str(path).replace("'", "''") + "')"
        return (
            "(SELECT " + columns + " FROM " + recorded + " UNION ALL " + live
            + " AND ts > (SELECT max(ts) FROM " + recorded + "))"
        )

    def _query(self, sql: str, params: list | None = None) -> list[tuple]:
        with self._lock:
            return self._con.execute(sql, params or []).fetchall()

    # ---- reads -----------------------------------------------------------
    def candles(
        self,
        symbol: str,
        timeframe: str,
        *,
        as_of: datetime,
        limit: int = 500,
    ) -> list[Candle]:
        """The most recent `limit` completed bars at or before `as_of`,
        returned oldest-first."""
        sql = (
            "SELECT ts, open, high, low, close, spread, volume FROM ("
            "  SELECT * FROM " + self._scan(symbol, timeframe)
            + "  WHERE ts <= ? ORDER BY ts DESC LIMIT ?"
            ") ORDER BY ts ASC"
        )
        rows = self._query(sql, [_completed_before(as_of, timeframe), int(limit)])
        return [
            Candle(r[0].replace(tzinfo=UTC), r[1], r[2], r[3], r[4], r[5] or 0.0, r[6] or 0.0)
            for r in rows
        ]

    def latest_close(self, symbol: str, timeframe: str, *, as_of: datetime) -> Candle | None:
        rows = self._query(
            "SELECT ts, open, high, low, close, spread, volume FROM "
            + self._scan(symbol, timeframe)
            + " WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
            [_completed_before(as_of, timeframe)],
        )
        if not rows:
            return None
        r = rows[0]
        return Candle(r[0].replace(tzinfo=UTC), r[1], r[2], r[3], r[4], r[5] or 0.0, r[6] or 0.0)

    def coverage(self, symbol: str, timeframe: str) -> Coverage:
        rows = self._query(
            "SELECT count(*), min(ts), max(ts) FROM " + self._scan(symbol, timeframe)
        )
        n, lo, hi = rows[0] if rows else (0, None, None)
        return Coverage(
            symbol=symbol,
            timeframe=timeframe,
            bars=n,
            first_bar=lo.replace(tzinfo=UTC) if lo else None,
            last_bar=hi.replace(tzinfo=UTC) if hi else None,
        )

    def _recorded_coverage(self, symbol: str, timeframe: str) -> Coverage:
        path = str(self.path_for(symbol, timeframe)).replace("'", "''")
        rows = self._query("SELECT count(*), min(ts), max(ts) FROM read_parquet('" + path + "')")
        n, lo, hi = rows[0] if rows else (0, None, None)
        return Coverage(
            symbol=symbol, timeframe=timeframe, bars=n,
            first_bar=lo.replace(tzinfo=UTC) if lo else None,
            last_bar=hi.replace(tzinfo=UTC) if hi else None,
        )

    def recorded_symbols(self) -> tuple[str, ...]:
        return tuple(
            s for s in universe.SYMBOLS
            if all(self.path_for(s, tf).exists() for tf in universe.TIMEFRAMES)
        )

    def store_bounds(self, timeframe: str = "M5") -> tuple[datetime | None, datetime | None]:
        """The span of moments the dashboard can be asked about: from the
        earliest bar's open to the latest bar's *close*, because that is when
        the newest bar becomes visible. The latest is the *minimum* of
        per-symbol maxima, so an instrument that stopped recording early
        cannot drag the dashboard's clock past its own coverage."""
        firsts: list[datetime] = []
        lasts: list[datetime] = []
        for symbol in self.recorded_symbols():
            cov = self._recorded_coverage(symbol, timeframe)
            if cov.first_bar and cov.last_bar:
                firsts.append(cov.first_bar)
                lasts.append(cov.last_bar)
        if not firsts:
            return None, None
        return max(firsts), min(lasts) + timedelta(minutes=universe.TIMEFRAMES[timeframe.upper()])

    def close_series(
        self,
        symbol: str,
        timeframe: str,
        *,
        as_of: datetime,
        limit: int,
    ) -> list[tuple[datetime, float]]:
        sql = (
            "SELECT ts, close FROM ("
            "  SELECT ts, close FROM " + self._scan(symbol, timeframe)
            + "  WHERE ts <= ? ORDER BY ts DESC LIMIT ?"
            ") ORDER BY ts ASC"
        )
        return [
            (r[0].replace(tzinfo=UTC), r[1])
            for r in self._query(sql, [_completed_before(as_of, timeframe), int(limit)])
        ]

    def close(self) -> None:
        with self._lock:
            self._con.close()


def _naive(ts: datetime) -> datetime:
    """DuckDB TIMESTAMP columns here are UTC-naive; compare like with like."""
    return ts.astimezone(UTC).replace(tzinfo=None) if ts.tzinfo else ts


def _completed_before(as_of: datetime, timeframe: str) -> datetime:
    """The latest bar *open* whose bar had finished by `as_of`."""
    return _naive(as_of) - timedelta(minutes=universe.TIMEFRAMES[timeframe.upper()])


@lru_cache(maxsize=1)
def get_store() -> CandleStore:
    return CandleStore()
