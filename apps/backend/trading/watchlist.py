"""Persistent market watchlist (mission: TARS Watcher Intelligence, section
2). Reuses `AppStateStore` (storage/app_state.py -- existing, tested, but
previously unwired) for durable key-value persistence rather than adding a
dedicated table: the watchlist is a small list, not a thing that needs its
own indexed queries, so a single JSON blob under one key keeps the schema
simple as the mission asks.

Every entry is the product of a successful `AssetResolver.resolve()` call --
this module never stores an unresolved or ambiguous alias; that disambiguation
happens one layer up (MarketWatchService) before an entry is ever built.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True)
class WatchEntry:
    asset: str  # what the user said, e.g. "gold" -- kept for display/recall
    canonical_symbol: str  # resolved ticker, e.g. "XAUUSD"
    provider_symbols: tuple[str, ...] = field(default_factory=tuple)
    added_at: str = ""
    enabled: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> WatchEntry:
        return WatchEntry(
            asset=data.get("asset", ""),
            canonical_symbol=data["canonical_symbol"],
            provider_symbols=tuple(data.get("provider_symbols") or (data["canonical_symbol"],)),
            added_at=data.get("added_at", ""),
            enabled=data.get("enabled", True),
        )


_WATCHLIST_KEY = "watchlist"


class WatchlistStore:
    """Thin, durable CRUD layer over AppStateStore -- one entry per
    canonical_symbol (adding an already-watched symbol updates its entry
    rather than duplicating it)."""

    def __init__(self, app_state_store, *, key: str = _WATCHLIST_KEY) -> None:
        self._store = app_state_store
        self._key = key

    async def load(self) -> list[WatchEntry]:
        raw = await self._store.get(self._key)
        if not raw:
            return []
        try:
            rows = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        return [WatchEntry.from_dict(row) for row in rows if isinstance(row, dict) and row.get("canonical_symbol")]

    async def _save(self, entries: list[WatchEntry]) -> None:
        await self._store.set(self._key, json.dumps([e.to_dict() for e in entries]))

    async def add(self, asset: str, canonical_symbol: str, *, provider_symbols: tuple[str, ...] = ()) -> WatchEntry:
        entries = await self.load()
        symbol = canonical_symbol.upper()
        entry = WatchEntry(
            asset=asset, canonical_symbol=symbol,
            provider_symbols=provider_symbols or (symbol,),
            added_at=datetime.now(UTC).isoformat(), enabled=True,
        )
        entries = [e for e in entries if e.canonical_symbol != symbol] + [entry]
        await self._save(entries)
        return entry

    async def remove(self, canonical_symbol: str) -> bool:
        entries = await self.load()
        symbol = canonical_symbol.upper()
        remaining = [e for e in entries if e.canonical_symbol != symbol]
        if len(remaining) == len(entries):
            return False
        await self._save(remaining)
        return True
