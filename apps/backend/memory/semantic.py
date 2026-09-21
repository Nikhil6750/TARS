"""Semantic operations on existing memory_notes. No provider or network calls.

The source sentence remains local provenance; retrieval returns canonical data.
The SQL trigger makes replacement and indexing a single atomic statement. Reads
always filter superseded facts. There is no cache of mutable semantic truth.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import aiosqlite

from memory.interpretation import (
    MAX_TEXT,
    SECRET,
    Fact,
    answer,
    clean,
    entity,
    interpret,
    recall_target,
    symbol,
)
from memory.session import SessionMemoryStore

MAX_RESULTS = 5


class SemanticMemory:
    def __init__(self, conn: aiosqlite.Connection, session: SessionMemoryStore):
        self.conn, self.session = conn, session

    @staticmethod
    def _scope(scope: str, conversation_id: str | None) -> str:
        if scope == "persistent":
            return scope
        if scope == "ephemeral" and conversation_id and len(conversation_id) <= 200:
            return f"session:{conversation_id}"
        raise ValueError("Memory scope must be persistent or a named ephemeral session")

    @staticmethod
    def _data(row) -> dict[str, Any]:
        result = json.loads(row["metadata"])
        result.update(id=row["note_id"], supersedes=row["supersedes"],
                      superseded_by=row["superseded_by"])
        return result

    async def _lookup(self, subject: str, relation: str, scope: str) -> list[dict]:
        if scope.startswith("session:"):
            return [f for f in self.session.semantic_facts(scope).values()
                    if f["subject"].casefold() == subject.casefold() and f["relation"] == relation]
        cursor = await self.conn.execute(
            "SELECT * FROM memory_notes WHERE semantic_key = ? AND superseded_by IS NULL",
            (f"{scope}:{subject.casefold()}:{relation}",),
        )
        return [self._data(row) for row in await cursor.fetchall()]

    async def remember(
        self, statement: str, *, actor: str = "user", conversation_id: str | None = None,
        channel: str = "text", scope: str = "persistent", explicit: bool = False,
        expected_id: str | None = None,
        _legacy_note: dict | None = None,
    ) -> dict:
        if actor != "user" or channel not in {"text", "gemini_live", "legacy_note"}:
            return {"status": "REJECTED", "reason": "Only explicit user knowledge can become memory."}
        if not isinstance(statement, str) or len(statement) > MAX_TEXT:
            return {"status": "REJECTED", "facts": [], "reason": "Memory statements must be at most 1000 characters."}
        scoped = self._scope(scope, conversation_id)
        names = await self._lookup("USER", "called", scoped)
        parsed = interpret(statement, explicit=explicit, user_name=names[0]["object"] if names else None)
        if parsed.status != "READY":
            return {"status": parsed.status, "facts": [], "reason": {
                "REJECTED": "I can't save secrets or invalid memory input.",
                "EPHEMERAL": "That is temporary context, so I won't keep it as a persistent fact.",
                "UNRECOGNIZED": "No persistent knowledge was requested.",
                "NEEDS_CLARIFICATION": "What single fact or preference should I remember?",
            }[parsed.status]}
        # Repeated slots in one utterance are ambiguous, even if it says 'actually'.
        if len({f.key(scoped) for f in parsed.facts}) != len(parsed.facts):
            return {"status": "NEEDS_CLARIFICATION", "facts": [],
                    "reason": "Which one value should I remember?"}
        inserts: list[tuple[Fact, dict | None]] = []
        facts: list[dict] = []
        for fact in parsed.facts:
            current = await self._lookup(fact.subject, fact.relation, scoped)
            old = current[0] if current else None
            if expected_id and (old is None or old["id"] != expected_id):
                return {"status": "CONFLICT", "facts": [], "reason": "That memory has changed. Recall it again."}
            if old and old["object"].casefold() == fact.object.casefold():
                facts.append(old)
                continue
            if old and not (parsed.correction or expected_id):
                return {"status": "NEEDS_CLARIFICATION", "facts": [old],
                        "reason": f"I have {old['object']} for {fact.relation.replace('_', ' ')}. "
                                  f"Should I replace it with {fact.object}? Say 'Actually, ...' with the correction."}
            inserts.append((fact, old))
        now = datetime.now(UTC).isoformat()
        rows = []
        for fact, old in inserts:
            identifier = uuid4().hex
            data: dict[str, Any] = {
                "subject": fact.subject, "relation": fact.relation, "object": fact.object,
                "memory_type": fact.memory_type, "confidence": 1.0, "scope": scope,
                "aliases": list(fact.aliases),
                "canonical_entities": {"subject": fact.subject, "object": fact.object},
                "created_at": now, "updated_at": now,
                "source": {"actor": "user", "channel": channel, "conversation_id": conversation_id,
                           "source_id": identifier, "basis": "explicit_user_statement"},
            }
            if _legacy_note:
                data["source"].update(legacy_note_id=_legacy_note["note_id"],
                                      original_created_at=_legacy_note["created_at"])
            previous = old["id"] if old else None
            rows.append((identifier, "explicit_memory", identifier, "user", conversation_id,
                         "[]", statement, json.dumps(data), now, fact.key(scoped), previous))
            facts.append({**data, "id": identifier, "supersedes": previous, "superseded_by": None})
        if scope == "ephemeral":
            for stored_fact in facts:
                self.session.set_semantic_fact(scoped, stored_fact)
        elif rows:
            try:
                # A single multi-row INSERT: the whole utterance succeeds or none of it does.
                await self.conn.execute(
                    "INSERT INTO memory_notes(note_id,kind,source_id,actor,conversation_id,tags,body,"
                    "metadata,created_at,semantic_key,supersedes) VALUES "
                    + ",".join("(?,?,?,?,?,?,?,?,?,?,?)" for _ in rows),
                    [item for row in rows for item in row],
                )
                await self.conn.commit()
            except sqlite3.IntegrityError:
                return {"status": "CONFLICT", "facts": [],
                        "reason": "That memory changed while saving. Recall it again before correcting it."}
        self.session.invalidate_retrieval()
        return {"status": "UPDATED" if any(old for _, old in inserts) else "SAVED" if rows else "UNCHANGED",
                "facts": facts}

    async def recall(
        self, query: str, *, limit: int = MAX_RESULTS, scope: str = "persistent",
        conversation_id: str | None = None,
    ) -> list[dict]:
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_TEXT or SECRET.search(query):
            return []
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("limit must be an integer")
        limit = max(1, min(limit, MAX_RESULTS))
        scoped = self._scope(scope, conversation_id)
        if target := recall_target(query):
            return (await self._lookup(*target, scoped))[:limit]
        if re.search(r"\b(?:default|preferred) chart\b", query, re.I):
            facts = await self._lookup("USER", "default_timeframe", scoped)
            facts += await self._lookup("USER", "default_symbol", scoped)
            return facts[:limit]
        # Canonical relation synonyms apply even to search-style queries.
        relations = []
        for relation, pattern in {
            "developed_by": r"\b(?:built|made|created|developed|developer|creator|maker)\b",
            "default_timeframe": r"\b(?:timeframe|time frame)\b",
            "default_symbol": r"\bdefault (?:symbol|instrument)\b",
            "works_on": r"\b(?:project|working on|work on)\b",
            "alias_of": r"\b(?:alias|means|mean|called)\b",
        }.items():
            if re.search(pattern, query, re.I):
                relations.append(relation)
        # Generic search uses normalized data ONLY, never source sentences/transcripts.
        tokens = re.findall(r"[\w]+", clean(query).lower())
        stop = {"who", "what", "is", "are", "the", "my", "your", "do", "did", "i", "a", "an",
                "about", "tell", "me", "remember", "memory", "search", "for", "check", "please"}
        keywords = [t for t in tokens if t not in stop]
        if relations:
            # Keep explicit entities to avoid answering 'who made Arun' with TARS's developer.
            terms = [t for t in keywords if t not in {
                "built", "made", "created", "developed", "developer", "creator", "maker", "by",
                "default", "preferred", "timeframe", "time", "frame", "project", "working", "work",
                "on", "alias", "means", "mean", "called", "symbol", "instrument", "s",
            }]
        else:
            terms = keywords
        terms = [entity(t).lower() for t in terms]
        if not relations and not terms:
            return []
        if scoped.startswith("session:"):
            candidates = list(self.session.semantic_facts(scoped).values())
            return [f for f in candidates if (not relations or f["relation"] in relations)
                    and all(t in f"{f['subject']} {f['object']}".lower() for t in terms)][:limit]
        sql = "SELECT * FROM memory_notes WHERE semantic_key IS NOT NULL AND superseded_by IS NULL"
        params: list[Any] = []
        if relations:
            sql += " AND json_extract(metadata, '$.relation') IN (" + ",".join("?" for _ in relations) + ")"
            params.extend(relations)
        for term in terms[:12]:
            sql += " AND (lower(json_extract(metadata, '$.subject')) LIKE ? ESCAPE '\\' OR " \
                   "lower(json_extract(metadata, '$.object')) LIKE ? ESCAPE '\\')"
            literal = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params.extend([literal, literal])
        sql += " ORDER BY created_at DESC, note_id LIMIT ?"
        params.append(limit)
        cursor = await self.conn.execute(sql, params)
        return [self._data(row) for row in await cursor.fetchall()]

    async def resolve_symbol(self, value: str) -> str:
        alias = await self._lookup(clean(value).lower(), "alias_of", "persistent")
        return alias[0]["object"] if alias else symbol(value) or clean(value).upper()

    async def respond(self, text: str, *, conversation_id: str | None = None) -> str | None:
        """Deterministic conversational gate; None leaves unrelated requests alone."""
        if target := recall_target(text):
            facts = await self._lookup(*target, "persistent")
            return answer(facts[0]) if facts else "I don't have that in memory yet."
        if re.fullmatch(r"(?:open|show) my default chart[.!?]?", text.strip(), re.I):
            facts = await self.recall(text)
            details = " ".join(answer(f) for f in facts)
            return (details + " Which chart should I open?" if details else
                    "I don't have a default chart timeframe or symbol yet.")
        parsed = interpret(text)
        if parsed.status == "UNRECOGNIZED":
            return None
        result = await self.remember(text, conversation_id=conversation_id)
        if result["status"] in {"SAVED", "UPDATED", "UNCHANGED"}:
            return "Remembered. " + " ".join(answer(f) for f in result["facts"])
        return result["reason"]

    async def forget(self, note_id: str) -> bool:
        # Forget the complete revision lineage; deleting a correction must not
        # resurrect a previously superseded claim or leave its source behind.
        cursor = await self.conn.execute(
            "SELECT semantic_key, superseded_by FROM memory_notes WHERE note_id = ?", (note_id,),
        )
        row = await cursor.fetchone()
        if row and row["semantic_key"] is None and row["superseded_by"]:
            cursor = await self.conn.execute(
                "SELECT semantic_key FROM memory_notes WHERE note_id = ?", (row["superseded_by"],),
            )
            row = await cursor.fetchone()
        if not row or row["semantic_key"] is None:
            return False
        await self.conn.execute(
            "DELETE FROM memory_notes WHERE note_id IN ("
            "SELECT json_extract(metadata, '$.source.legacy_note_id') FROM memory_notes WHERE semantic_key = ?"
            ") OR semantic_key = ?", (row["semantic_key"], row["semantic_key"]),
        )
        await self.conn.commit()
        self.session.invalidate_retrieval()
        return True

    async def import_legacy(self) -> int:
        """One startup pass, in small pages; never promotes transcripts/agent notes.

        Unrecognized old notes remain notes. Existing structured facts win over
        old text even when that text contains a historical correction marker.
        """
        # Preflight all legacy slot values before promotion. UUID ordering must
        # never decide which of two conflicting old statements is "true".
        values: dict[str, set[str]] = {}
        async for row in self._legacy_rows():
            parsed = interpret(row["body"], explicit=True)
            if parsed.status == "READY":
                for fact in parsed.facts:
                    values.setdefault(fact.key("persistent"), set()).add(fact.object.casefold())
        imported = 0
        async for row in self._legacy_rows():
            parsed = interpret(row["body"], explicit=True)
            if parsed.status != "READY":
                continue
            existing = [await self._lookup(f.subject, f.relation, "persistent") for f in parsed.facts]
            conflict = any(len(values[f.key("persistent")]) > 1 for f in parsed.facts)
            conflict = conflict or any(
                old and old[0]["object"].casefold() != fact.object.casefold()
                for fact, old in zip(parsed.facts, existing, strict=True)
            )
            if conflict:
                # Preserve the evidence locally, but do not feed an unresolved
                # legacy claim back into the ordinary model grounding index.
                await self.conn.execute(
                    "UPDATE memory_notes SET metadata=json_set(metadata, '$.semantic_import_status', 'conflict') "
                    "WHERE note_id=?", (row["note_id"],),
                )
                await self.conn.execute(
                    "DELETE FROM memory_fts WHERE source='explicit_memory' AND source_id=?", (row["note_id"],),
                )
                await self.conn.commit()
                continue
            if any(existing):
                continue
            result = await self.remember(
                row["body"], conversation_id=row["conversation_id"], channel="legacy_note",
                explicit=True, _legacy_note=dict(row),
            )
            imported += result["status"] == "SAVED"
        self.session.invalidate_retrieval()
        return imported

    async def _legacy_rows(self):
        after = ""
        while True:
            cursor = await self.conn.execute(
                "SELECT * FROM memory_notes WHERE kind='explicit_memory' AND actor='user' "
                "AND semantic_key IS NULL AND superseded_by IS NULL AND note_id > ? "
                "ORDER BY note_id LIMIT 100", (after,),
            )
            rows = await cursor.fetchall()
            if not rows:
                return
            for row in rows:
                after = row["note_id"]
                yield row
