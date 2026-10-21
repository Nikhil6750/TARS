"""Parquet(ZSTD) datasets + DuckDB analytical queries. Storage sits behind this small interface so the
provider/format can change without touching research code."""
from __future__ import annotations

import hashlib
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .schema import ARROW_SCHEMA, CANONICAL_COLUMNS


class MarketStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _dir(self, provider: str, instrument: str, timeframe: str) -> Path:
        return self.root / "market" / provider / instrument.upper() / timeframe

    def write_bars(self, bars: pd.DataFrame, merge: bool = False, replace_range: tuple | None = None) -> list[Path]:
        """One parquet file per (provider, instrument, timeframe, year). merge=False replaces the year file;
        merge=True keeps existing rows OUTSIDE the incoming time range and splices the new rows in (so a bounded
        re-ingest never destroys other months of the same year). replace_range=(start, end) widens the replaced window to the
        whole REQUESTED range [start, end) so rows a newer normalisation no longer emits cannot linger from older ingests."""
        first = bars.iloc[0]
        d = self._dir(first["provider"], first["instrument"], first["source_timeframe"])
        d.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for year, part in bars.groupby(bars["timestamp_utc"].dt.year):
            path = d / f"{year}.parquet"
            if merge and path.exists():
                old = pq.read_table(path).to_pandas()
                old["timestamp_utc"] = pd.to_datetime(old["timestamp_utc"], utc=True)
                lo, hi = part["timestamp_utc"].min(), part["timestamp_utc"].max()
                if replace_range is not None:
                    lo = min(lo, pd.Timestamp(replace_range[0]).tz_convert("UTC"))
                    hi_excl = pd.Timestamp(replace_range[1]).tz_convert("UTC")
                    keep = old[(old["timestamp_utc"] < lo) | (old["timestamp_utc"] >= max(hi_excl, hi + pd.Timedelta(1, "ns")))]
                else:
                    keep = old[(old["timestamp_utc"] < lo) | (old["timestamp_utc"] > hi)]
                part = pd.concat([keep, part], ignore_index=True).sort_values("timestamp_utc", kind="stable").reset_index(drop=True)
            table = pa.Table.from_pandas(part[list(CANONICAL_COLUMNS)], schema=ARROW_SCHEMA, preserve_index=False)
            pq.write_table(table, path, compression="zstd")
            written.append(path)
        return written

    def files(self, provider: str, instrument: str, timeframe: str) -> list[Path]:
        return sorted(self._dir(provider, instrument, timeframe).glob("*.parquet"))

    def read_bars(self, provider: str, instrument: str, timeframe: str = "M1",
                  start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> pd.DataFrame:
        files = self.files(provider, instrument, timeframe)
        if not files:
            return pd.DataFrame(columns=list(CANONICAL_COLUMNS))
        sql = "SELECT * FROM read_parquet(?)"
        params: list = [[str(f) for f in files]]
        cond = []
        if start is not None:
            cond.append("timestamp_utc >= ?")
            params.append(pd.Timestamp(start).tz_convert("UTC").to_pydatetime())
        if end is not None:
            cond.append("timestamp_utc < ?")
            params.append(pd.Timestamp(end).tz_convert("UTC").to_pydatetime())
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY timestamp_utc"
        con = duckdb.connect()
        try:
            df = con.execute(sql, params).df()
        finally:
            con.close()
        df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
        return df

    def query(self, sql: str, params: list | None = None) -> pd.DataFrame:
        """Ad-hoc analytical SQL on an in-memory DuckDB (reference files with read_parquet('...'))."""
        con = duckdb.connect()
        try:
            return con.execute(sql, params or []).df()
        finally:
            con.close()

    @staticmethod
    def data_version(bars: pd.DataFrame) -> str:
        """Content hash of the exact rows an experiment used (order-sensitive)."""
        frame = bars[list(CANONICAL_COLUMNS)].copy()
        frame["timestamp_utc"] = frame["timestamp_utc"].astype("int64")
        h = pd.util.hash_pandas_object(frame, index=False).values
        return "dv_" + hashlib.sha256(h.tobytes()).hexdigest()[:16]
