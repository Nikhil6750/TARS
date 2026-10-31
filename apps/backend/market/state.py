"""Durable state for the Market X-Ray: zones, zone-interaction events,
currency-strength history, feed health and the versioned calculation config.

Written to its own SQLite database so the dashboard never shares a schema or a
write lock with the existing TARS companion store. Large candle history stays
in Parquet/DuckDB (see `market.store`); only state and events live here.

Idempotency is the point of this module. Zones are keyed by
``(symbol, timeframe, side, origin_ts)`` and events by a digest of
``(zone_id, kind, ts)``, both enforced by the schema, so re-scanning the same
history -- on a restart, a replay rewind, or a duplicate WebSocket push --
updates rows in place instead of appending near-duplicates. That is what makes
acceptance test 9 ("event history does not duplicate on restart") hold by
construction rather than by hope.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("tars.market.state")

DEFAULT_DB = Path("apps/backend/storage/market_xray.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calc_config (
    version      TEXT PRIMARY KEY,
    payload      TEXT NOT NULL,
    first_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS zones (
    zone_id          TEXT PRIMARY KEY,
    symbol           TEXT NOT NULL,
    timeframe        TEXT NOT NULL,
    side             TEXT NOT NULL,
    zone_low         REAL NOT NULL,
    zone_high        REAL NOT NULL,
    origin_ts        TEXT NOT NULL,
    confirmed_ts     TEXT NOT NULL,
    displacement_atr REAL NOT NULL,
    bos_level        REAL NOT NULL,
    status           TEXT NOT NULL,
    score            REAL NOT NULL,
    config_version   TEXT NOT NULL,
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL,
    UNIQUE (symbol, timeframe, side, origin_ts)
);
CREATE INDEX IF NOT EXISTS idx_zones_symbol ON zones (symbol, timeframe, status);

CREATE TABLE IF NOT EXISTS zone_events (
    event_id   TEXT PRIMARY KEY,
    zone_id    TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    timeframe  TEXT NOT NULL,
    kind       TEXT NOT NULL,
    ts         TEXT NOT NULL,
    price      REAL NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (zone_id, kind, ts)
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON zone_events (ts DESC);

CREATE TABLE IF NOT EXISTS strength_history (
    as_of     TEXT NOT NULL,
    window    TEXT NOT NULL,
    currency  TEXT NOT NULL,
    score     REAL NOT NULL,
    pairs_used INTEGER NOT NULL,
    thin_coverage INTEGER NOT NULL,
    PRIMARY KEY (as_of, window, currency)
);

CREATE TABLE IF NOT EXISTS feed_health (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    status       TEXT NOT NULL,
    market_open  INTEGER NOT NULL,
    data_as_of   TEXT,
    detail       TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    symbol     TEXT NOT NULL,
    timeframe  TEXT NOT NULL,
    as_of      TEXT NOT NULL,
    close      REAL NOT NULL,
    pct_change REAL NOT NULL,
    z          REAL NOT NULL,
    atr        REAL,
    regime     TEXT,
    PRIMARY KEY (symbol, timeframe)
);
"""


class MarketState:
    def __init__(self, db_path: Path | str = DEFAULT_DB):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ---- config ----------------------------------------------------------
    def record_config(self, version: str, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO calc_config (version, payload, first_seen) VALUES (?, ?, ?) "
                "ON CONFLICT (version) DO UPDATE SET payload = excluded.payload",
                (version, json.dumps(payload, sort_keys=True), _now()),
            )
            self._conn.commit()

    # ---- zones and events ------------------------------------------------
    def upsert_zones(self, zones: list) -> int:
        """Insert or refresh zones. Returns the number of rows written."""
        if not zones:
            return 0
        now = _now()
        rows = [
            (
                z.zone_id, z.symbol, z.timeframe, z.side, z.zone_low, z.zone_high,
                z.origin_ts, z.confirmed_ts, z.displacement_atr, z.bos_level,
                z.status, z.score, z.config_version, now, now,
            )
            for z in zones
        ]
        with self._lock:
            self._conn.executemany(
                "INSERT INTO zones (zone_id, symbol, timeframe, side, zone_low, zone_high,"
                " origin_ts, confirmed_ts, displacement_atr, bos_level, status, score,"
                " config_version, first_seen, last_seen)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT (zone_id) DO UPDATE SET"
                "   status = excluded.status, score = excluded.score,"
                "   zone_low = excluded.zone_low, zone_high = excluded.zone_high,"
                "   last_seen = excluded.last_seen",
                rows,
            )
            self._conn.commit()
        return len(rows)

    def record_events(self, events: list) -> int:
        """Persist zone events, ignoring any already stored. Returns the count
        of genuinely new rows."""
        if not events:
            return 0
        now = _now()
        rows = [
            (e.event_id, e.zone_id, e.symbol, e.timeframe, e.kind, e.ts, e.price, now)
            for e in events
        ]
        with self._lock:
            before = self._conn.execute("SELECT count(*) FROM zone_events").fetchone()[0]
            self._conn.executemany(
                "INSERT INTO zone_events (event_id, zone_id, symbol, timeframe, kind, ts,"
                " price, recorded_at) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT (event_id) DO NOTHING",
                rows,
            )
            self._conn.commit()
            after = self._conn.execute("SELECT count(*) FROM zone_events").fetchone()[0]
        return after - before

    def recent_events(self, *, symbol: str | None = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM zone_events"
        params: list = []
        if symbol:
            sql += " WHERE symbol = ?"
            params.append(symbol.upper())
        sql += " ORDER BY ts DESC, kind ASC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def event_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT count(*) FROM zone_events").fetchone()[0]

    def zone_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT count(*) FROM zones").fetchone()[0]

    # ---- strength history ------------------------------------------------
    def record_strength(self, snapshot) -> None:
        rows = [
            (snapshot.as_of, snapshot.window, r.currency, r.score, r.pairs_used,
             1 if r.thin_coverage else 0)
            for r in snapshot.ranking
        ]
        with self._lock:
            self._conn.executemany(
                "INSERT INTO strength_history (as_of, window, currency, score, pairs_used,"
                " thin_coverage) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT (as_of, window, currency) DO UPDATE SET score = excluded.score",
                rows,
            )
            self._conn.commit()

    def strength_history(self, currency: str, window: str, limit: int = 200) -> list[dict]:
        with self._lock:
            return [
                dict(r)
                for r in self._conn.execute(
                    "SELECT as_of, score, pairs_used FROM strength_history"
                    " WHERE currency = ? AND window = ? ORDER BY as_of DESC LIMIT ?",
                    (currency.upper(), window, int(limit)),
                ).fetchall()
            ]

    # ---- snapshots and feed health ---------------------------------------
    def record_snapshots(self, rows: list[tuple]) -> None:
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO snapshots (symbol, timeframe, as_of, close, pct_change, z, atr,"
                " regime) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT (symbol, timeframe) DO UPDATE SET"
                "   as_of = excluded.as_of, close = excluded.close,"
                "   pct_change = excluded.pct_change, z = excluded.z,"
                "   atr = excluded.atr, regime = excluded.regime",
                rows,
            )
            self._conn.commit()

    def record_feed_health(self, status) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO feed_health (id, status, market_open, data_as_of, detail, updated_at)"
                " VALUES (1,?,?,?,?,?)"
                " ON CONFLICT (id) DO UPDATE SET status = excluded.status,"
                "   market_open = excluded.market_open, data_as_of = excluded.data_as_of,"
                "   detail = excluded.detail, updated_at = excluded.updated_at",
                (
                    status.status.value,
                    1 if status.market_open else 0,
                    status.data_as_of.isoformat() if status.data_as_of else None,
                    status.detail,
                    _now(),
                ),
            )
            self._conn.commit()

    def feed_health(self) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM feed_health WHERE id = 1").fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _now() -> str:
    return datetime.now(UTC).isoformat()
