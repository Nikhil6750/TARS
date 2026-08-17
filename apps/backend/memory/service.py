"""Memory/retrieval service — the single entry point the rest of the
backend uses for indexing and searching memory. Enforces the boundary from
ARCHITECTURE.md § Memory architecture: operational state (trading_events,
active_setups) is never indexed here, conversation memory and vault
research notes are searchable but are never treated as validated trading
performance, and every result carries its source + source_id so an
assistant answer can cite exactly where it came from.
"""
from __future__ import annotations

from typing import Any

import aiosqlite

from app.observability import get_tracer
from app.schemas import AssistantMessage
from memory import fts, notes
from memory.session import SessionMemoryStore
from memory.vault import VaultIndexResult, reindex_vault

tracer = get_tracer()

# Memory-note kinds this service writes to `memory_notes` (see
# storage/migrations/0002_tars_core_memory_agents.sql). Kept as a closed set
# so callers can't invent an ad-hoc kind string that search/list can't find.
KIND_EXPLICIT_MEMORY = "explicit_memory"
KIND_TRADING_OBSERVATION = "trading_observation"
KIND_DECISION = "decision"


class MemoryService:
    def __init__(
        self,
        conn: aiosqlite.Connection,
        vault_path: str,
        sqlite_vec_enabled: bool,
        session: SessionMemoryStore | None = None,
    ):
        self._conn = conn
        self._vault_path = vault_path
        self._session = session or SessionMemoryStore()
        if sqlite_vec_enabled:
            # ADR-013: sqlite-vec is added only if FTS5 relevance proves
            # insufficient in practice, not introduced speculatively. No
            # measurement has justified it yet, so refuse rather than
            # silently ignoring the flag.
            raise NotImplementedError(
                "SQLITE_VEC_ENABLED=true but semantic retrieval is not yet "
                "implemented — see ARCHITECTURE.md § Memory architecture "
                "and DECISIONS.md ADR-013. Set it back to false."
            )

    @property
    def session(self) -> SessionMemoryStore:
        """Short-term, in-process conversation memory — see memory/session.py."""
        return self._session

    async def index_conversation_message(self, message: AssistantMessage) -> None:
        title = f"{message.role.value} @ {message.timestamp.isoformat()}"
        await fts.upsert(
            self._conn,
            source="conversation",
            source_id=str(message.message_id),
            title=title,
            body=message.content,
        )
        self._session.remember_turn(
            str(message.conversation_id), message.role.value, message.content
        )

    async def reindex_vault(self) -> VaultIndexResult:
        return await reindex_vault(self._conn, self._vault_path)

    async def search(
        self,
        query: str,
        limit: int = 10,
        source: str | None = None,
        *,
        use_cache: bool = False,
    ) -> list[dict[str, Any]]:
        cache_key = f"{source or 'any'}:{limit}:{query.strip().lower()}"
        if use_cache:
            cached = self._session.get_cached_retrieval(cache_key)
            if cached is not None:
                return cached
        with tracer.start_as_current_span("memory.search") as span:
            span.set_attribute("memory.query_length", len(query))
            span.set_attribute("memory.source_filter", source or "any")
            results = await fts.search(self._conn, query=query, limit=limit, source=source)
            # Retrieved context identifiers, per ARCHITECTURE.md § Observability —
            # logs which notes/turns backed an answer, never their content.
            span.set_attribute(
                "memory.retrieved_ids", [f"{r['source']}:{r['source_id']}" for r in results]
            )
            span.set_attribute("memory.result_count", len(results))
        if use_cache:
            self._session.cache_retrieval(cache_key, results)
        return results

    # ---- Structured, provenance-carrying notes (memory_notes) ----------

    async def remember(
        self,
        text: str,
        *,
        actor: str = "user",
        conversation_id: str | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """Explicit "remember this" — the user (or orchestrator, on the
        user's behalf) asked TARS to durably keep a fact. Indexed for search
        and recorded with provenance (who said to remember it, and when)."""
        return await self._save_note(
            kind=KIND_EXPLICIT_MEMORY,
            body=text,
            actor=actor,
            conversation_id=conversation_id,
            tags=tags,
            title_prefix="Remembered",
        )

    async def save_trading_observation(
        self,
        text: str,
        *,
        symbol: str | None = None,
        actor: str = "user",
        conversation_id: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """A qualitative trading observation (from the user or an agent),
        never a validated trading fact — see MemoryService.search's
        docstring boundary. `symbol` is optional grounding, not a claim that
        quant_brain validated anything about it."""
        return await self._save_note(
            kind=KIND_TRADING_OBSERVATION,
            body=text,
            actor=actor,
            conversation_id=conversation_id,
            symbol=symbol,
            tags=tags,
            metadata=metadata,
            title_prefix=f"Trading observation{f' — {symbol}' if symbol else ''}",
        )

    async def save_decision(
        self,
        text: str,
        *,
        actor: str = "system",
        conversation_id: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """A task/decision record — what TARS (or an agent acting for the
        user) decided and why. Used for later "why did you do X" recall."""
        return await self._save_note(
            kind=KIND_DECISION,
            body=text,
            actor=actor,
            conversation_id=conversation_id,
            tags=tags,
            metadata=metadata,
            title_prefix="Decision",
        )

    async def list_notes(
        self,
        kind: str,
        *,
        symbol: str | None = None,
        conversation_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return await notes.list_by_kind(
            self._conn, kind, symbol=symbol, conversation_id=conversation_id, limit=limit
        )

    async def forget(self, note_id: str) -> bool:
        """Deletes a structured note and its FTS entry. Returns False if no
        such note existed (idempotent, not an error)."""
        note = await notes.get(self._conn, note_id)
        if note is None:
            return False
        await notes.delete(self._conn, note_id)
        # FTS rows for a note are indexed with source=kind, source_id=note_id
        # (see _save_note) -- not source=note_id, which would never match.
        await fts.delete(self._conn, source=note["kind"], source_id=note_id)
        return True

    async def _save_note(
        self,
        *,
        kind: str,
        body: str,
        actor: str,
        title_prefix: str,
        conversation_id: str | None = None,
        symbol: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        note_id = await notes.insert(
            self._conn,
            kind=kind,
            actor=actor,
            body=body,
            conversation_id=conversation_id,
            symbol=symbol,
            tags=tags,
            metadata=metadata,
        )
        # `source` == kind, `source_id` == note_id, so an FTS hit resolves
        # straight back to the structured record via MemoryService.get_note.
        await fts.upsert(
            self._conn,
            source=kind,
            source_id=note_id,
            title=f"{title_prefix} @ {note_id}",
            body=body,
        )
        return note_id

    async def get_note(self, note_id: str) -> dict[str, Any] | None:
        return await notes.get(self._conn, note_id)
