"""Local historical M1 BID + ASK CSV provider (e.g. C:\\Trade Data\\xauusd_utc).

The source folder is IMMUTABLE: files are read in place, hashed, never copied or modified. Parsing goes through DuckDB as raw
text first, so malformed rows, duplicate timestamps and one-sided (bid-only / ask-only) timestamps are COUNTED, never coerced,
forward-filled or fabricated. Processing is chunked by calendar year so memory stays bounded.

Expected layout per file (detected, not assumed):  header `Time (UTC),Open,High,Low,Close,Volume`, timestamps `YYYY.MM.DD HH:MM:SS`."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from ..instruments import get_meta
from ..schema import make_bars
from .base import MarketDataProvider, RawCache, RawFile
from .dukascopy import drop_dead_padding

NORMALIZATION_VERSION = "local-csv-m1-v2"  # v2: crossed bid/ask bars quarantined (counted) instead of imported
NAME_RE = re.compile(r"^(?P<inst>[A-Za-z]+)_1 Min_(?P<side>Bid|Ask)_(?P<start>[\d.]+)_(?P<end>[\d.]+)\.csv$", re.IGNORECASE)
EXPECTED_HEADER = ["time (utc)", "open", "high", "low", "close", "volume"]
TS_FORMAT = "%Y.%m.%d %H:%M:%S"
OVERLAP_DAYS = 2   # context rows around each year chunk so padding-run detection never sees an artificial chunk edge


class LocalDataError(RuntimeError):
    pass


def sha256_file(path: Path, chunk: int = 8 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


@dataclass
class SourceFile:
    path: Path
    side: str            # BID | ASK
    instrument: str
    size: int
    header: list[str]
    delimiter: str
    sha256: str = ""
    rows: int = 0
    first_ts: str = ""
    last_ts: str = ""


@dataclass
class JoinReport:
    bid_rows: int = 0
    ask_rows: int = 0
    malformed_bid: int = 0
    malformed_ask: int = 0
    duplicate_bid: int = 0
    duplicate_ask: int = 0
    bid_only: int = 0
    ask_only: int = 0
    joined: int = 0
    bid_gt_ask: int = 0
    quarantined_samples: list = field(default_factory=list)
    crossed_quarantined_ts: list = field(default_factory=list)   # bid>ask bars excluded from the canonical set (counted + listed)


class LocalCsvProvider(MarketDataProvider):
    name = "local_xauusd"

    def __init__(self, source_dir: str | Path, instrument: str = "XAUUSD", name: str | None = None) -> None:
        self.source_dir = Path(source_dir)
        self.instrument = instrument.upper()
        if name:
            self.name = name
        self.sources: dict[str, SourceFile] = {}
        self.join = JoinReport()
        self.last_normalization_stats: dict = {}
        self.last_fetch_stats: dict = {}
        self._con: duckdb.DuckDBPyConnection | None = None

    # ---- discovery (nothing assumed) -----------------------------------------------------------------
    def discover(self, hash_files: bool = True) -> dict[str, SourceFile]:
        found: dict[str, SourceFile] = {}
        for p in sorted(self.source_dir.glob("*.csv")):
            m = NAME_RE.match(p.name)
            if not m or m.group("inst").upper() != self.instrument:
                continue
            side = m.group("side").upper()
            if side in found:
                raise LocalDataError(f"more than one {side} file for {self.instrument}: {found[side].path.name}, {p.name}")
            with open(p, "r", encoding="utf-8-sig", errors="strict") as f:
                first = f.readline().rstrip("\r\n")
            delim = max(",;\t|", key=first.count)
            header = [h.strip().lower() for h in first.split(delim)]
            if header != EXPECTED_HEADER:
                raise LocalDataError(f"{p.name}: unexpected header {header}")
            found[side] = SourceFile(p, side, self.instrument, p.stat().st_size, header, delim, sha256_file(p) if hash_files else "")
        if set(found) != {"BID", "ASK"}:
            raise LocalDataError(f"need exactly one BID and one ASK file for {self.instrument} in {self.source_dir}; found {sorted(found)}")
        self.sources = found
        return found

    # ---- contract ------------------------------------------------------------------------------------
    def estimate_download_bytes(self, instrument: str, start: datetime, end: datetime) -> int:
        return 0

    def fetch(self, instrument: str, start: datetime, end: datetime, cache: RawCache | None = None) -> list[RawFile]:
        """No download: the immutable source files are referenced in place."""
        if not self.sources:
            self.discover()
        return [RawFile(s.path, f"{s.side}", str(s.path), s.sha256, s.size, "local") for s in self.sources.values()]

    def describe_source(self, instrument: str) -> dict:
        if not self.sources:
            self.discover(hash_files=False)
        return {"provider": self.name, "source_dir": str(self.source_dir), "source_format": "CSV, 1-minute OHLCV, separate BID and ASK files",
                "timestamps": "UTC, 'YYYY.MM.DD HH:MM:SS' (verified against closure/open edges and an independent Dukascopy feed)",
                "normalization_version": NORMALIZATION_VERSION, "price_scale": "decimal prices as published",
                "volume": "bid-side volume as published (not exchange volume)",
                "files": [{"side": s.side, "name": s.path.name, "size": s.size, "sha256": s.sha256, "rows": s.rows,
                           "first_ts": s.first_ts, "last_ts": s.last_ts, "delimiter": s.delimiter} for s in self.sources.values()],
                "immutable_source": True,
                "caveats": ["export of retail/indicative quotes", "closed-market padding (flat zero-volume rows) may be present and is dropped by an explicit counted rule"]}

    # ---- staging in DuckDB (raw text -> typed, with explicit rejects) -----------------------------------------
    def _load(self) -> None:
        if self._con is not None:
            return
        if not self.sources:
            self.discover()
        con = duckdb.connect()
        for side, src in self.sources.items():
            t = side.lower()
            con.execute(f"""CREATE TABLE raw_{t} AS SELECT * FROM read_csv(?, header=true, delim=?, quote='"', all_varchar=true, ignore_errors=false,
                            columns={{'t':'VARCHAR','o':'VARCHAR','h':'VARCHAR','l':'VARCHAR','c':'VARCHAR','v':'VARCHAR'}})""",
                        [str(src.path), src.delimiter])
            con.execute(f"""CREATE TABLE typed_{t} AS SELECT try_strptime(t, '{TS_FORMAT}') AS ts, try_cast(o AS DOUBLE) AS o, try_cast(h AS DOUBLE) AS h,
                            try_cast(l AS DOUBLE) AS l, try_cast(c AS DOUBLE) AS c, try_cast(v AS DOUBLE) AS v, t AS raw_t FROM raw_{t}""")
            bad = con.execute(f"SELECT count(*) FROM typed_{t} WHERE ts IS NULL OR o IS NULL OR h IS NULL OR l IS NULL OR c IS NULL OR v IS NULL").fetchone()[0]
            dup = con.execute(f"SELECT count(*) - count(DISTINCT ts) FROM typed_{t} WHERE ts IS NOT NULL").fetchone()[0]
            n, lo, hi = con.execute(f"SELECT count(*), min(ts), max(ts) FROM typed_{t}").fetchone()
            src.rows, src.first_ts, src.last_ts = int(n), str(lo), str(hi)
            setattr(self.join, f"{t}_rows", int(n))
            setattr(self.join, f"malformed_{t}", int(bad))
            setattr(self.join, f"duplicate_{t}", int(dup))
            # clean = parsed rows, first occurrence of each timestamp (duplicates are COUNTED above and reported, not hidden)
            con.execute(f"""CREATE TABLE clean_{t} AS SELECT * EXCLUDE rn FROM (SELECT *, row_number() OVER (PARTITION BY ts ORDER BY rowid) rn
                            FROM typed_{t} WHERE ts IS NOT NULL AND o IS NOT NULL AND h IS NOT NULL AND l IS NOT NULL AND c IS NOT NULL AND v IS NOT NULL) WHERE rn = 1""")
            con.execute(f"DROP TABLE raw_{t}")
        j = self.join
        j.bid_only, j.ask_only, j.joined = con.execute(
            """SELECT count(*) FILTER (WHERE a.ts IS NULL), count(*) FILTER (WHERE b.ts IS NULL), count(*) FILTER (WHERE a.ts IS NOT NULL AND b.ts IS NOT NULL)
               FROM clean_bid b FULL OUTER JOIN clean_ask a ON a.ts = b.ts""").fetchone()
        j.bid_gt_ask = con.execute("SELECT count(*) FROM clean_bid b JOIN clean_ask a USING (ts) WHERE b.c > a.c OR b.o > a.o OR b.h > a.h OR b.l > a.l").fetchone()[0]
        j.crossed_quarantined_ts = [str(x[0]) for x in con.execute(
            """SELECT ts FROM clean_bid b JOIN clean_ask a USING (ts) WHERE b.c > a.c OR b.o > a.o OR b.h > a.h OR b.l > a.l ORDER BY ts LIMIT 200""").fetchall()]
        j.quarantined_samples = [str(x[0]) for x in con.execute(
            """SELECT coalesce(a.ts, b.ts) FROM clean_bid b FULL OUTER JOIN clean_ask a ON a.ts = b.ts WHERE a.ts IS NULL OR b.ts IS NULL ORDER BY 1 LIMIT 50""").fetchall()]
        self._con = con

    def year_range(self) -> tuple[int, int]:
        self._load()
        lo, hi = self._con.execute("SELECT min(ts), max(ts) FROM (SELECT ts FROM clean_bid UNION SELECT ts FROM clean_ask)").fetchone()
        return lo.year, hi.year

    def _frame(self, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        """Joined rows with start <= ts < end. ONE-SIDED timestamps are excluded from the canonical frame (counted in JoinReport and
        sampled for the report); a bar needs both sides, and nothing is forward-filled or fabricated."""
        self._load()
        df = self._con.execute(
            """SELECT b.ts AS timestamp_utc, b.o AS bid_open, b.h AS bid_high, b.l AS bid_low, b.c AS bid_close,
                      a.o AS ask_open, a.h AS ask_high, a.l AS ask_low, a.c AS ask_close, b.v AS volume
               FROM clean_bid b JOIN clean_ask a USING (ts)
               WHERE b.ts >= ? AND b.ts < ? AND a.o >= b.o AND a.h >= b.h AND a.l >= b.l AND a.c >= b.c   -- crossed quotes are quarantined, not repaired
               ORDER BY b.ts""",
            [start.tz_localize(None).to_pydatetime(), end.tz_localize(None).to_pydatetime()]).df()
        df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"]).dt.tz_localize("UTC")
        df["spread"] = df["ask_close"] - df["bid_close"]
        return make_bars(df, instrument=self.instrument, provider=self.name)

    def iter_years(self):
        """Yield (year, normalised_bars, padding_stats). Padding detection uses +-OVERLAP_DAYS of context, then trims to the year."""
        y0, y1 = self.year_range()
        meta = get_meta(self.instrument)
        for y in range(y0, y1 + 1):
            lo, hi = pd.Timestamp(y, 1, 1, tz="UTC"), pd.Timestamp(y + 1, 1, 1, tz="UTC")
            ctx = self._frame(lo - pd.Timedelta(days=OVERLAP_DAYS), hi + pd.Timedelta(days=OVERLAP_DAYS))
            if ctx.empty:
                continue
            kept, stats = drop_dead_padding(ctx, meta)
            kept = kept[(kept["timestamp_utc"] >= lo) & (kept["timestamp_utc"] < hi)].reset_index(drop=True)
            in_year = ctx[(ctx["timestamp_utc"] >= lo) & (ctx["timestamp_utc"] < hi)]
            stats = {**stats, "rows_in_year": int(len(in_year)), "rows_out_year": int(len(kept)), "rows_dropped_year": int(len(in_year) - len(kept))}
            yield y, kept, stats

    def normalize(self, instrument: str, raw: list[RawFile]) -> pd.DataFrame:
        """Whole-dataset convenience (memory heavy for 16 years; the import driver streams iter_years instead)."""
        parts, agg = [], {"rows_dropped": 0}
        for _, kept, st in self.iter_years():
            parts.append(kept)
            agg["rows_dropped"] += st["rows_dropped_year"]
        self.last_normalization_stats = agg
        return pd.concat(parts, ignore_index=True) if parts else make_bars(pd.DataFrame(columns=["timestamp_utc"]), instrument=self.instrument, provider=self.name)

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None
