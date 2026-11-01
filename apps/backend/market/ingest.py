"""Bid/ask CSV -> validated mid-price Parquet candle store.

Source of truth is the recorded M1 bid and ask CSVs on this machine. For each
instrument the two sides are joined on their UTC minute, converted to mid
prices, and aggregated into the dashboard timeframes with DuckDB.

Two rules make the output safe to compute signals from:

* **Mid from both sides.** A bar exists only where *both* a bid and an ask
  observation exist for that minute, so every candle carries a real spread
  rather than an inferred one.
* **Completed buckets only.** The final bucket of each timeframe is dropped
  unless its whole span lies inside the source data's coverage, so no
  partially-formed bar ever reaches a calculation.

Both sides are also fully OHLC-validated before aggregation. The recorded
CSVs do contain a small number of malformed minutes (observed: bars on
2024-10-10 where ``High < Open`` on one side), and those are rejected rather
than averaged into a mid bar; the rejected count is reported per instrument
in the manifest instead of being silently dropped. Aggregate highs/lows are
then clamped to envelope the bucket's own open/close, which removes
floating-point noise from the mid-price arithmetic.

Nothing is written outside `store_root`. The frozen Quant Agent research
partitions (``<data root>/market/local_xauusd``, ``.../dukascopy``) are never
written to and never modified.

Neither root is hardcoded (task P1-C2). The store lives under
`research.config.resolve_data_root()`, which is this repo's existing
`TARS_DATA_ROOT` convention -- reusing it rather than restating it means the
market store follows the same rule as every other dataset, including its
refusal to fill the system drive. The CSV source root is overridable with
`TARS_MARKET_CSV_ROOT`, because a machine that holds its history elsewhere
should not need a code change to ingest.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from market import universe
from research.config import resolve_data_root

logger = logging.getLogger("tars.market.ingest")

# Where the source bid/ask CSVs live. `C:\Trade Data` is where they sit on
# the machine this was built on, kept as the fallback so nothing changes for
# it, but overridable for any machine that stores them elsewhere.
_DEFAULT_CSV_ROOT = Path("C:/Trade Data")


def csv_root() -> Path:
    env = os.environ.get("TARS_MARKET_CSV_ROOT", "").strip()
    return Path(env) if env else _DEFAULT_CSV_ROOT


def default_store_root() -> Path:
    r"""The candle store, under the repo's shared data root.

    `resolve_data_root()` honours TARS_DATA_ROOT, else picks a non-system
    drive with room to spare, else falls back inside the repo. On the
    machine this was developed on it returns `D:\TARS_DATA`, so this is the
    same path as the constant it replaces.
    """
    return resolve_data_root() / "market" / "xray_store"


# Resolved once at import: these are defaults for arguments, and a root that
# changed under a running ingest would write half the bars to each.
CSV_ROOT = csv_root()
DEFAULT_STORE_ROOT = default_store_root()
# Everything before this is recorded history the dashboard does not need;
# keeping the window bounded is what makes a full ingest take seconds.
DEFAULT_START = "2024-01-01"
# Publishing takes moments, so a wait this long means something is wrong.
PUBLICATION_LOCK_SECONDS = 60.0

_CSV_COLUMNS = {
    "Time (UTC)": "TIMESTAMP",
    "Open": "DOUBLE",
    "High": "DOUBLE",
    "Low": "DOUBLE",
    "Close": "DOUBLE",
    "Volume": "DOUBLE",
}


@dataclass
class IngestResult:
    symbol: str
    timeframe: str
    bars: int
    first_bar: str | None
    last_bar: str | None
    source_last_minute: str | None
    path: str
    # Per-instrument source quality, repeated on each timeframe row so the
    # manifest stays flat and queryable.
    source_m1_accepted: int = 0
    source_m1_rejected: int = 0


# A bar is usable only if it is internally consistent on its own side.
_VALID_SIDE = (
    "{a}.High >= {a}.Low AND {a}.High >= {a}.Open AND {a}.High >= {a}.Close "
    "AND {a}.Low <= {a}.Open AND {a}.Low <= {a}.Close AND {a}.Low > 0"
)


def _csv_path(meta: universe.Instrument, side: str, root: Path | None = None) -> Path:
    """Resolve the one CSV for this instrument/side, or raise naming what was
    actually searched. Never silently falls back to another instrument."""
    directory = (root or CSV_ROOT) / meta.source_dir
    matches = sorted(directory.glob(meta.source_stem + "_" + side + "_*.csv"))
    if not matches:
        raise FileNotFoundError("no " + side + " CSV for " + meta.symbol + " under " + str(directory))
    return matches[-1]


def _csv_scan(path: Path) -> str:
    literal = str(path).replace("'", "''")
    return (
        "read_csv('" + literal + "', header=true, "
        "timestampformat='%Y.%m.%d %H:%M:%S', "
        "columns=" + json.dumps(_CSV_COLUMNS) + ", ignore_errors=false)"
    )


def ingest_symbol(
    con: duckdb.DuckDBPyConnection,
    symbol: str,
    *,
    store_root: Path = DEFAULT_STORE_ROOT,
    start: str = DEFAULT_START,
    csv_root: Path | None = None,
) -> list[IngestResult]:
    meta = universe.get(symbol)
    bid, ask = _csv_path(meta, "Bid", csv_root), _csv_path(meta, "Ask", csv_root)

    # Every minute quoted on both sides, before validity filtering, so the
    # rejection rate can be measured rather than guessed.
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE joined AS
        SELECT b."Time (UTC)" AS ts,
               (b.Open  + a.Open ) / 2 AS open,
               (b.High  + a.High ) / 2 AS high,
               (b.Low   + a.Low  ) / 2 AS low,
               (b.Close + a.Close) / 2 AS close,
               a.Close - b.Close        AS spread,
               COALESCE(b.Volume, 0) + COALESCE(a.Volume, 0) AS volume,
               ("""
        + _VALID_SIDE.format(a="b")
        + " AND "
        + _VALID_SIDE.format(a="a")
        + """ AND a.Close >= b.Close) AS usable
        FROM """ + _csv_scan(bid) + """ b
        JOIN """ + _csv_scan(ask) + """ a USING ("Time (UTC)")
        WHERE b."Time (UTC)" >= TIMESTAMP '""" + start + """'
        """
    )
    quality = con.execute(
        "SELECT count(*) FILTER (WHERE usable), count(*) FILTER (WHERE NOT usable) FROM joined"
    ).fetchone()
    accepted, rejected = (quality or (0, 0))
    con.execute("CREATE OR REPLACE TEMP TABLE m1 AS SELECT * FROM joined WHERE usable ORDER BY ts")

    bounds = con.execute("SELECT max(ts) FROM m1").fetchone()
    source_last = bounds[0] if bounds else None
    if source_last is None:
        raise ValueError(symbol + ": no usable bid/ask minutes at or after " + start)
    if rejected:
        logger.info("%s: rejected %d malformed source minutes of %d", symbol, rejected, accepted + rejected)

    results: list[IngestResult] = []
    for timeframe, minutes in universe.TIMEFRAMES.items():
        out_dir = store_root / symbol
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / (timeframe + ".parquet")
        literal = str(out).replace("'", "''")
        span = str(minutes) + " minutes"
        con.execute(
            """
            COPY (
                SELECT
                    time_bucket(INTERVAL '""" + span + """', ts) AS ts,
                    arg_min(open, ts)  AS open,
                    -- clamped to envelope open/close, removing float noise
                    -- introduced by the (bid+ask)/2 arithmetic.
                    greatest(max(high), arg_min(open, ts), arg_max(close, ts)) AS high,
                    least(min(low), arg_min(open, ts), arg_max(close, ts))     AS low,
                    arg_max(close, ts) AS close,
                    avg(spread)        AS spread,
                    sum(volume)        AS volume,
                    count(*)           AS m1_bars
                FROM m1
                GROUP BY 1
                HAVING time_bucket(INTERVAL '""" + span + """', ts)
                       + INTERVAL '""" + span + """' - INTERVAL '1 minute'
                       <= TIMESTAMP '""" + str(source_last) + """'
                ORDER BY 1
            ) TO '""" + literal + """' (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )
        stats = con.execute(
            "SELECT count(*), min(ts), max(ts) FROM read_parquet('" + literal + "')"
        ).fetchone()
        n, lo, hi = stats if stats else (0, None, None)
        results.append(
            IngestResult(
                symbol=symbol,
                timeframe=timeframe,
                bars=n,
                first_bar=lo.replace(tzinfo=UTC).isoformat() if lo else None,
                last_bar=hi.replace(tzinfo=UTC).isoformat() if hi else None,
                source_last_minute=source_last.replace(tzinfo=UTC).isoformat(),
                path=str(out),
                source_m1_accepted=accepted,
                source_m1_rejected=rejected,
            )
        )
    return results


@contextmanager
def _publication_lock(store_root: Path) -> Generator[None]:
    """One publisher at a time, across processes, from reading the active
    manifest to replacing it.

    An atomic rename prevents a torn manifest, not a lost update: two imports
    extending the same manifest would each discard what the other added. The
    operating system drops SQLite's file lock when its holder dies, so a killed
    import cannot block the next one, as a marker file would.
    """
    con = sqlite3.connect(
        store_root / ".publication.lock",
        timeout=PUBLICATION_LOCK_SECONDS,
        isolation_level=None,
    )
    try:
        try:
            con.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as exc:
            raise TimeoutError(
                "another import is still publishing; active data unchanged"
            ) from exc
        yield
    finally:
        con.close()


def _carried_datasets(
    store_root: Path, generation_root: Path, rebuilt: set[str], failures: dict[str, str]
) -> list[dict]:
    """Bring everything active that this import did not rebuild into its new
    generation: symbols it was not asked for, and ones it failed to replace,
    whose old data is still the best there is. Files are immutable, so linking
    is safe; a copy covers filesystems without hardlinks."""
    old_manifest = store_root / "manifest.json"
    if not old_manifest.exists():
        return []
    previous = json.loads(old_manifest.read_text(encoding="utf-8"))
    old_root = store_root / previous.get("generation", "")
    carried: list[dict] = []
    for prior in previous.get("datasets", []):
        symbol = prior.get("symbol")
        tf = prior.get("timeframe")
        if symbol in rebuilt or not symbol or not tf:
            continue
        source = old_root / symbol / (tf + ".parquet")
        if not source.is_file():
            continue
        destination = generation_root / symbol / (tf + ".parquet")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)
        carried.append({**prior, "path": str(destination)})
    for symbol in failures.keys() & {row["symbol"] for row in carried}:
        failures[symbol] += "; previous data kept"
    return carried


def ingest_all(
    symbols: list[str] | None = None,
    *,
    store_root: Path = DEFAULT_STORE_ROOT,
    start: str = DEFAULT_START,
    csv_root: Path | None = None,
) -> dict:
    """Ingest every requested instrument, recording per-symbol failures rather
    than aborting the run, so partial coverage is reported and never hidden."""
    targets = symbols or list(universe.SYMBOLS)
    store_root.mkdir(parents=True, exist_ok=True)
    # Immutable generations make publishing an import atomic for all
    # timeframes/symbols, including readers in another process. Never replace
    # the Parquet files referenced by the active manifest.
    revision = uuid.uuid4().hex
    generation_name = f"generations/{revision}"
    generation_root = store_root / generation_name
    generation_root.mkdir(parents=True)
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")

    started = time.time()
    rows: list[dict] = []
    failures: dict[str, str] = {}
    for symbol in targets:
        t0 = time.time()
        try:
            for r in ingest_symbol(
                con, symbol, store_root=generation_root, start=start, csv_root=csv_root
            ):
                rows.append(asdict(r))
            logger.info("ingested %s in %.1fs", symbol, time.time() - t0)
        except Exception as exc:  # noqa: BLE001 - recorded in the manifest, not swallowed
            failures[symbol] = type(exc).__name__ + ": " + str(exc)
            logger.warning("ingest failed for %s: %s", symbol, exc)
            # Whatever it wrote before failing must not reach the generation.
            shutil.rmtree(generation_root / symbol, ignore_errors=True)
    con.close()

    if not rows:
        shutil.rmtree(generation_root)
        raise ValueError("import produced no usable candle datasets; active data unchanged")

    rebuilt = {r["symbol"] for r in rows}
    published = False
    try:
        with _publication_lock(store_root):
            # A clean full import replaces everything, and needs nothing from
            # the active generation, which is what lets it repair a bad manifest.
            if symbols is not None or failures:
                rows += _carried_datasets(store_root, generation_root, rebuilt, failures)
            manifest = {
                "dataset_revision": revision,
                "generation": generation_name,
                "generated_at": datetime.now(UTC).isoformat(),
                "csv_root": str(csv_root or CSV_ROOT),
                "store_root": str(store_root),
                "window_start": start,
                "timeframes": universe.TIMEFRAMES,
                "elapsed_seconds": round(time.time() - started, 2),
                "symbols_ok": sorted({r["symbol"] for r in rows} - failures.keys()),
                "symbols_failed": failures,
                "datasets": rows,
            }
            # Publish a single pointer only after every successful dataset is
            # written. A failed import leaves the previous generation and
            # manifest untouched.
            staged_manifest = store_root / f".manifest-{revision}.tmp"
            try:
                with staged_manifest.open("w", encoding="utf-8") as stream:
                    json.dump(manifest, stream, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(staged_manifest, store_root / "manifest.json")
                published = True
            finally:
                staged_manifest.unlink(missing_ok=True)
    except BaseException:
        if not published:
            shutil.rmtree(generation_root, ignore_errors=True)
        raise
    # Keep previous immutable generations: in-flight readers may still be
    # using them. Reclamation needs a separate, explicitly safe policy.
    return manifest


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Build the Market X-Ray candle store.")
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--store-root", default=str(DEFAULT_STORE_ROOT))
    args = ap.parse_args()
    m = ingest_all(args.symbols, store_root=Path(args.store_root), start=args.start)
    print("")
    print("ok=" + str(len(m["symbols_ok"])) + " failed=" + str(len(m["symbols_failed"]))
          + " in " + str(m["elapsed_seconds"]) + "s")
    for sym, err in m["symbols_failed"].items():
        print("  FAILED " + sym + ": " + err)
