"""Raw -> canonical ingestion pipeline with provenance.

provider raw -> immutable raw cache -> normalize -> canonical M1 bid/ask -> DataQualityReport -> Parquet/ZSTD -> DuckDB catalog

Nothing is repaired: suspicious data is stored as received and the quality report decides whether experiments may use it."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

from .providers.base import MarketDataProvider, RawCache
from .quality import DataQualityReport, gap_summary, validate_bars
from .storage import MarketStore


class StorageGuardError(RuntimeError):
    pass


def check_space(path: Path, need_bytes: int, reserve_bytes: int | None = None) -> dict:
    """Refuse to proceed if free space after the planned write would fall below the reserve."""
    if reserve_bytes is None:
        reserve_bytes = int(float(os.environ.get("TARS_DATA_RESERVE_GB", "5")) * 1e9)
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    info = {"free_bytes": free, "need_bytes": need_bytes, "reserve_bytes": reserve_bytes}
    if free - need_bytes < reserve_bytes:
        raise StorageGuardError(f"insufficient space: free={free / 1e9:.2f}GB need~{need_bytes / 1e9:.3f}GB "
                                f"reserve={reserve_bytes / 1e9:.1f}GB")
    return info


@dataclass
class IngestResult:
    rows: int
    quality: DataQualityReport
    provenance: dict
    provenance_path: Path
    parquet_files: list[Path]


def _dir_size(paths) -> int:
    return sum(Path(p).stat().st_size for p in paths)


def ingest(provider: MarketDataProvider, instrument: str, start: datetime, end: datetime, root: Path, *,
           reserve_bytes: int | None = None, max_download_bytes: int = 2_000_000_000) -> IngestResult:
    root = Path(root)
    est = provider.estimate_download_bytes(instrument, start, end)
    if est > max_download_bytes:
        raise StorageGuardError(f"estimated download {est / 1e6:.0f}MB exceeds the per-run cap {max_download_bytes / 1e6:.0f}MB")
    space = check_space(root, est * 4, reserve_bytes)  # raw + parquet + slack

    cache = RawCache(root / "market" / "_raw")
    retrieved = datetime.now(timezone.utc).isoformat()
    raw = provider.fetch(instrument, start, end, cache)
    bars = provider.normalize(instrument, raw)
    report = validate_bars(bars, timeframe="M1") if len(bars) else validate_bars(bars)

    store = MarketStore(root)
    files = store.write_bars(bars, merge=True, replace_range=(start, end)) if len(bars) else []
    digest = hashlib.sha256("".join(sorted(f"{r.key}:{r.sha256}" for r in raw)).encode()).hexdigest()
    prov = {
        "provider": provider.name, "instrument": instrument.upper(), "timeframe": "M1",
        "retrieved_at": retrieved, "requested_range": [start.isoformat(), end.isoformat()],
        "actual_range": [str(bars["timestamp_utc"].min()), str(bars["timestamp_utc"].max())] if len(bars) else None,
        "row_count": int(len(bars)), "raw_files": len(raw), "raw_bytes": int(sum(r.size for r in raw)),
        "raw_checksum_sha256": digest, "parquet_bytes": _dir_size(files) if files else 0,
        "canonical_data_version": MarketStore.data_version(bars) if len(bars) else None,
        "source": provider.describe_source(instrument), "storage_check": space,
        "normalization": dict(getattr(provider, "last_normalization_stats", {}) or {}),
        "quality": {"passed": report.passed, "gap_categories": gap_summary(report),
                    "issues": [(i.code, i.severity, i.category, i.count) for i in report.issues]},
    }
    pdir = root / "market" / "_provenance"
    pdir.mkdir(parents=True, exist_ok=True)
    name = f"{provider.name}_{instrument.upper()}_M1_{start:%Y%m%d}_{end:%Y%m%d}.json"
    ppath = pdir / name
    ppath.write_text(json.dumps(prov, indent=2, default=str), encoding="utf-8")
    if len(bars):
        register_duckdb(root, provider.name, instrument)
    return IngestResult(len(bars), report, prov, ppath, files)


def register_duckdb(root: Path, provider: str, instrument: str) -> Path:
    """Persistent DuckDB catalog with one view per dataset over the Parquet files (data stays in Parquet)."""
    store = MarketStore(root)
    files = store.files(provider, instrument, "M1")
    cat = Path(root) / "derived" / "catalog.duckdb"
    cat.parent.mkdir(parents=True, exist_ok=True)
    view = f"{instrument.lower()}_m1_{provider.lower()}"
    con = duckdb.connect(str(cat))
    try:
        listing = ", ".join("'" + str(f).replace("\\", "/") + "'" for f in files)
        con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet([{listing}])")
    finally:
        con.close()
    return cat
