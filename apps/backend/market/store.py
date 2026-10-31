"""Read-only DuckDB access to the Market X-Ray candle store.

Every read is bounded by an explicit ``as_of`` timestamp and only ever returns
bars whose close time is at or before it. That single rule is what keeps the
whole dashboard free of look-ahead: replaying to an earlier moment cannot
reveal a bar that had not completed yet, because the filter is applied in SQL
rather than trimmed afterwards.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import duckdb

from market import universe
from market.ingest import DEFAULT_STORE_ROOT

logger = logging.getLogger("tars.market.store")


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
        self._lock = threading.Lock()
        self._con = duckdb.connect(database=":memory:")
        self._con.execute("SET TimeZone='UTC'")
        self.manifest = self._load_manifest()

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
        return self.root / symbol.upper() / (timeframe.upper() + ".parquet")

    def has(self, symbol: str, timeframe: str) -> bool:
        return self.path_for(symbol, timeframe).exists()

    @property
    def available_symbols(self) -> tuple[str, ...]:
        """Symbols in the data-backed universe that actually have files on
        disk for every advertised timeframe."""
        return tuple(
            s for s in universe.SYMBOLS
            if all(self.has(s, tf) for tf in universe.TIMEFRAMES)
        )

    def _scan(self, symbol: str, timeframe: str) -> str:
        path = self.path_for(symbol, timeframe)
        if not path.exists():
            raise FileNotFoundError("no candles for " + symbol + " " + timeframe)
        return "read_parquet('" + str(path).replace("'", "''") + "')"

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
        rows = self._query(sql, [_naive(as_of), int(limit)])
        return [
            Candle(r[0].replace(tzinfo=UTC), r[1], r[2], r[3], r[4], r[5] or 0.0, r[6] or 0.0)
            for r in rows
        ]

    def latest_close(self, symbol: str, timeframe: str, *, as_of: datetime) -> Candle | None:
        rows = self._query(
            "SELECT ts, open, high, low, close, spread, volume FROM "
            + self._scan(symbol, timeframe)
            + " WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
            [_naive(as_of)],
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

    def store_bounds(self, timeframe: str = "M5") -> tuple[datetime | None, datetime | None]:
        """Earliest and latest bar available across every symbol at this
        timeframe. The latest is the *minimum* of per-symbol maxima, so an
        instrument that stopped recording early cannot drag the dashboard's
        clock past its own coverage."""
        firsts: list[datetime] = []
        lasts: list[datetime] = []
        for symbol in self.available_symbols:
            cov = self.coverage(symbol, timeframe)
            if cov.first_bar and cov.last_bar:
                firsts.append(cov.first_bar)
                lasts.append(cov.last_bar)
        if not firsts:
            return None, None
        return max(firsts), min(lasts)

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
            for r in self._query(sql, [_naive(as_of), int(limit)])
        ]

    def close(self) -> None:
        with self._lock:
            self._con.close()


def _naive(ts: datetime) -> datetime:
    """DuckDB TIMESTAMP columns here are UTC-naive; compare like with like."""
    return ts.astimezone(UTC).replace(tzinfo=None) if ts.tzinfo else ts


@lru_cache(maxsize=1)
def get_store() -> CandleStore:
    return CandleStore()
