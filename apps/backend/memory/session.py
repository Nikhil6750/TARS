"""In-process short-term/session memory — the fast, ephemeral layer above
`MemoryService`'s durable SQLite-backed memory (`memory_notes` +
`memory_fts`). Holds a small rolling window of "what's been salient in this
conversation recently" per conversation_id so the orchestrator can ground a
reply without a full FTS search on every turn.

Deliberately NOT persisted and NOT a source of trading facts — it is a
cache of recently-seen text, not evidence of anything, per
ARCHITECTURE.md's memory-layer boundaries. Process restart clears it, which
is fine: durable facts live in `memory_notes`/`memory_fts`, this is purely a
latency/relevance optimization (see § Performance in the TARS core spec —
"cache memory retrieval").
"""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass
class SessionEntry:
    text: str
    role: str  # "user" | "assistant" | "system"
    added_at: float = field(default_factory=time.monotonic)


class SessionMemoryStore:
    """Bounded per-conversation ring buffer plus a tiny cross-request
    retrieval-result cache. Both are capped so a long-running process can't
    accumulate unbounded memory for abandoned conversations."""

    def __init__(
        self,
        *,
        max_entries_per_conversation: int = 20,
        max_conversations: int = 200,
        retrieval_cache_ttl_seconds: float = 30.0,
        max_retrieval_cache_entries: int = 256,
    ) -> None:
        self._conversations: OrderedDict[str, list[SessionEntry]] = OrderedDict()
        self._max_entries = max_entries_per_conversation
        self._max_conversations = max_conversations
        self._retrieval_cache: OrderedDict[str, tuple[float, list[dict]]] = OrderedDict()
        self._retrieval_ttl = retrieval_cache_ttl_seconds
        self._max_retrieval_cache = max_retrieval_cache_entries

    def remember_turn(self, conversation_id: str, role: str, text: str) -> None:
        if not text.strip():
            return
        entries = self._conversations.setdefault(conversation_id, [])
        entries.append(SessionEntry(text=text, role=role))
        if len(entries) > self._max_entries:
            del entries[: -self._max_entries]
        self._conversations.move_to_end(conversation_id)
        while len(self._conversations) > self._max_conversations:
            self._conversations.popitem(last=False)

    def recent(self, conversation_id: str, limit: int = 10) -> list[SessionEntry]:
        entries = self._conversations.get(conversation_id, [])
        return entries[-limit:]

    def clear(self, conversation_id: str) -> None:
        self._conversations.pop(conversation_id, None)

    def cache_retrieval(self, cache_key: str, results: list[dict]) -> None:
        self._retrieval_cache[cache_key] = (time.monotonic(), results)
        self._retrieval_cache.move_to_end(cache_key)
        while len(self._retrieval_cache) > self._max_retrieval_cache:
            self._retrieval_cache.popitem(last=False)

    def get_cached_retrieval(self, cache_key: str) -> list[dict] | None:
        entry = self._retrieval_cache.get(cache_key)
        if entry is None:
            return None
        stored_at, results = entry
        if time.monotonic() - stored_at > self._retrieval_ttl:
            del self._retrieval_cache[cache_key]
            return None
        return results
