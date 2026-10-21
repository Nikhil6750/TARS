"""MarketDataProvider contract. The research engine only ever sees canonical bars; vendors live behind this interface.

    fetch(...)           -> raw files, written ONCE into an immutable raw cache (never overwritten)
    normalize(...)       -> canonical M1 bid/ask DataFrame (no value repair)
    describe_source(...) -> provenance description (vendor, format, scale, caveats)
"""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class RawFile:
    path: Path
    key: str            # provider-specific identity, e.g. "2025-03-13/BID"
    url: str
    sha256: str
    size: int
    retrieved_at: str   # ISO UTC


class RawCache:
    """Write-once cache. An existing file is never replaced; a checksum mismatch raises."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_for(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def put(self, rel: tuple[str, ...], payload: bytes) -> tuple[Path, str]:
        path = self.path_for(*rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(payload).hexdigest()
        if path.exists():
            existing = hashlib.sha256(path.read_bytes()).hexdigest()
            if existing != digest:
                raise RuntimeError(f"raw cache is immutable: {path} already exists with different content")
            return path, digest
        path.write_bytes(payload)
        return path, digest


class MarketDataProvider(ABC):
    name: str

    @abstractmethod
    def estimate_download_bytes(self, instrument: str, start: datetime, end: datetime) -> int:
        """Upper-bound estimate used by the storage-space guard BEFORE any download."""

    @abstractmethod
    def fetch(self, instrument: str, start: datetime, end: datetime, cache: RawCache) -> list[RawFile]:
        ...

    @abstractmethod
    def normalize(self, instrument: str, raw: list[RawFile]) -> pd.DataFrame:
        ...

    @abstractmethod
    def describe_source(self, instrument: str) -> dict:
        ...
