"""Parquet(ZSTD) datasets + DuckDB analytical queries. Storage sits behind this small interface so the
provider/format can change without touching research code.

Layout: market/<provider>/<INSTRUMENT>/<TIMEFRAME>/<YYYY>/<MM>.parquet  (one file per calendar month, source M1 bid+ask).
Legacy single-file years (<YYYY>.parquet) are migrated to months transparently on the next write.

FINAL_UNTOUCHED protection: if experiments/research_split.json designates a final window for an instrument, read_bars()
refuses any read reaching into it (or unbounded) unless authorized_final=True, and every authorised read is appended to
experiments/final_access.jsonl."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .schema import ARROW_SCHEMA, CANONICAL_COLUMNS


class FinalDataAccessDenied(PermissionError):
    """Raised when code tries to read the FINAL_UNTOUCHED window without explicit promotion to final testing."""


def _utc(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


class MarketStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # ---- provider aliases (READ side only) -----------------------------------------------------------------
    def provider_alias_info(self, provider: str, instrument: str) -> dict | None:
        """experiments/provider_aliases.json lets a frozen ResearchSpec that names one provider resolve to another dataset WITHOUT
        mutating the spec. Each alias carries its justification/verification and is surfaced in evidence packets."""
        p = self.root / "experiments" / "provider_aliases.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("aliases", {}).get(instrument.upper(), {}).get(provider)
        except ValueError:
            return None

    def resolve_provider(self, provider: str, instrument: str) -> str:
        info = self.provider_alias_info(provider, instrument)
        return info["to"] if info else provider

    # ---- layout ---------------------------------------------------------------------------------------
    def _dir(self, provider: str, instrument: str, timeframe: str) -> Path:
        return self.root / "market" / provider / instrument.upper() / timeframe

    def files(self, provider: str, instrument: str, timeframe: str,
              start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> list[Path]:
        """Parquet files, optionally pruned to those whose month/year can overlap [start, end)."""
        d = self._dir(self.resolve_provider(provider, instrument), instrument, timeframe)
        out = []
        for p in sorted(d.rglob("*.parquet")):
            if start is not None or end is not None:
                lo, hi = self._file_span(p)
                if (end is not None and lo >= _utc(end)) or (start is not None and hi <= _utc(start)):
                    continue
            out.append(p)
        return out

    @staticmethod
    def _file_span(p: Path) -> tuple[pd.Timestamp, pd.Timestamp]:
        if p.stem.isdigit() and len(p.stem) == 4:      # legacy yearly file
            y = int(p.stem)
            return pd.Timestamp(y, 1, 1, tz="UTC"), pd.Timestamp(y + 1, 1, 1, tz="UTC")
        y, m = int(p.parent.name), int(p.stem)
        lo = pd.Timestamp(y, m, 1, tz="UTC")
        return lo, lo + pd.offsets.MonthBegin(1)

    # ---- write ------------------------------------------------------------------------------------------
    def _migrate_legacy_year(self, d: Path, year: int) -> None:
        legacy = d / f"{year}.parquet"
        if not legacy.exists():
            return
        old = pq.read_table(legacy).to_pandas()
        old["timestamp_utc"] = pd.to_datetime(old["timestamp_utc"], utc=True)
        for month, part in old.groupby(old["timestamp_utc"].dt.month):
            target = d / f"{year}" / f"{month:02d}.parquet"
            target.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(pa.Table.from_pandas(part[list(CANONICAL_COLUMNS)], schema=ARROW_SCHEMA, preserve_index=False),
                           target, compression="zstd")
        legacy.unlink()

    def write_bars(self, bars: pd.DataFrame, merge: bool = False, replace_range: tuple | None = None) -> list[Path]:
        """One parquet file per calendar month. merge=False replaces month files outright; merge=True keeps existing rows
        OUTSIDE the incoming window and splices the new rows in. replace_range=(start, end) widens the replaced window to the
        whole REQUESTED range [start, end) so rows a newer normalisation no longer emits cannot linger from older ingests."""
        first = bars.iloc[0]
        d = self._dir(first["provider"], first["instrument"], first["source_timeframe"])
        d.mkdir(parents=True, exist_ok=True)
        for year in bars["timestamp_utc"].dt.year.unique():
            self._migrate_legacy_year(d, int(year))
        written: list[Path] = []
        keys = [bars["timestamp_utc"].dt.year, bars["timestamp_utc"].dt.month]
        for (year, month), part in bars.groupby(keys):
            path = d / f"{year}" / f"{month:02d}.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            if merge and path.exists():
                old = pq.read_table(path).to_pandas()
                old["timestamp_utc"] = pd.to_datetime(old["timestamp_utc"], utc=True)
                lo, hi = part["timestamp_utc"].min(), part["timestamp_utc"].max()
                hi_excl = hi + pd.Timedelta(1, "ns")
                if replace_range is not None:
                    lo = min(lo, _utc(replace_range[0]))
                    hi_excl = max(hi_excl, _utc(replace_range[1]))
                keep = old[(old["timestamp_utc"] < lo) | (old["timestamp_utc"] >= hi_excl)]
                part = pd.concat([keep, part], ignore_index=True).sort_values("timestamp_utc", kind="stable").reset_index(drop=True)
            pq.write_table(pa.Table.from_pandas(part[list(CANONICAL_COLUMNS)], schema=ARROW_SCHEMA, preserve_index=False),
                           path, compression="zstd")
            written.append(path)
        return written

    # ---- FINAL_UNTOUCHED protection -----------------------------------------------------------------------
    def _split_path(self) -> Path:
        return self.root / "experiments" / "research_split.json"

    def final_start(self, instrument: str) -> pd.Timestamp | None:
        p = self._split_path()
        if not p.exists():
            return None
        try:
            spec = json.loads(p.read_text(encoding="utf-8"))["instruments"].get(instrument.upper())
        except (KeyError, ValueError):
            return None
        return _utc(spec["final_untouched"][0]) if spec else None

    def _guard(self, instrument: str, start, end, authorized_final: bool, purpose: str) -> None:
        fs = self.final_start(instrument)
        if fs is None:
            return
        reaches = end is None or _utc(end) > fs
        if not reaches:
            return
        if not authorized_final:
            raise FinalDataAccessDenied(f"{instrument}: read reaches the FINAL_UNTOUCHED window (from {fs}); "
                                        "this is reserved for explicit final testing")
        log = self.root / "experiments" / "final_access.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "instrument": instrument.upper(), "purpose": purpose,
                                "start": str(start), "end": str(end)}) + "\n")

    # ---- read --------------------------------------------------------------------------------------------
    def read_bars(self, provider: str, instrument: str, timeframe: str = "M1",
                  start: pd.Timestamp | None = None, end: pd.Timestamp | None = None, *,
                  authorized_final: bool = False, purpose: str = "research") -> pd.DataFrame:
        self._guard(instrument, start, end, authorized_final, purpose)
        files = self.files(provider, instrument, timeframe, start, end)
        if not files:
            return pd.DataFrame(columns=list(CANONICAL_COLUMNS))
        sql = "SELECT * FROM read_parquet(?)"
        params: list = [[str(f) for f in files]]
        cond = []
        if start is not None:
            cond.append("timestamp_utc >= ?")
            params.append(_utc(start).to_pydatetime())
        if end is not None:
            cond.append("timestamp_utc < ?")
            params.append(_utc(end).to_pydatetime())
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

    def read_timestamps(self, provider: str, instrument: str, timeframe: str = "M1") -> pd.Series:
        """Timestamps ONLY (no prices) for the whole dataset: enough for calendar/gap audits without exposing the final
        window's price action. Safe to use on the protected range."""
        files = self.files(provider, instrument, timeframe)
        if not files:
            return pd.Series([], dtype="datetime64[ns, UTC]")
        con = duckdb.connect()
        try:
            df = con.execute("SELECT timestamp_utc FROM read_parquet(?) ORDER BY 1", [[str(f) for f in files]]).df()
        finally:
            con.close()
        return pd.to_datetime(df["timestamp_utc"], utc=True)

    def query(self, sql: str, params: list | None = None) -> pd.DataFrame:
        """Operator-level ad-hoc SQL (bypasses the FINAL_UNTOUCHED guard; research code must use read_bars)."""
        con = duckdb.connect()
        try:
            return con.execute(sql, params or []).df()
        finally:
            con.close()

    @staticmethod
    def data_version(bars: pd.DataFrame) -> str:
        """Content hash of the exact rows an experiment used (order-sensitive)."""
        frame = bars[list(CANONICAL_COLUMNS)].copy()
        frame["timestamp_utc"] = frame["timestamp_utc"].dt.tz_convert("UTC").dt.tz_localize(None).astype("datetime64[ns]").astype("int64")
        h = pd.util.hash_pandas_object(frame, index=False).values
        return "dv_" + hashlib.sha256(h.tobytes()).hexdigest()[:16]
