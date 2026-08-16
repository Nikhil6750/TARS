"""Regression tests for the memory-grounding certification blocker:
`assistant.grounding.build_system_context` dropped `source_id` (the
Obsidian file path / conversation message_id) from retrieved memory notes,
keeping only the generic `source` type ("vault"/"conversation"). That broke
source traceability — an answer grounded in a specific note could never be
attributed back to which note. It also covers the "absent information"
requirement: when nothing relevant is in state or retrieved evidence, the
grounding context must make that explicit rather than let a model guess.
"""
from __future__ import annotations

import json

import aiosqlite
import pytest

from assistant.conversation_store import ConversationStore
from assistant.grounding import build_system_context
from assistant.provider import AssistantProvider, AssistantReply, AssistantRequest
from assistant.router import AssistantRouter
from events.service import EventService
from memory import fts
from storage.migrator import run_migrations


def _payload(context: str) -> dict:
    return json.loads(context.split("CURRENT STATE:\n", 1)[1])


def test_build_system_context_preserves_source_id_for_retrieved_notes():
    notes = [
        {"source": "vault", "source_id": "notes/risk.md", "snippet": "Never risk more than 1%."},
        {"source": "conversation", "source_id": "msg-42", "snippet": "earlier turn"},
    ]
    context = build_system_context([], memory_notes=notes)
    retrieved = _payload(context)["retrieved_memory_notes"]
    assert retrieved[0]["source"] == "vault"
    assert retrieved[0]["source_id"] == "notes/risk.md"
    assert retrieved[1]["source_id"] == "msg-42"
    # The provider is told to cite source_id, not just the generic source type.
    assert "source_id" in context


def test_build_system_context_with_no_data_discloses_unavailability():
    context = build_system_context([], memory_notes=[])
    payload = _payload(context)
    assert payload["active_setups"] == []
    assert "retrieved_memory_notes" not in payload
    assert "don't have that information" in context


class _EchoProvider(AssistantProvider):
    """Test double that reflects back exactly what grounding context it was
    given, so a test can assert on what AssistantRouter actually passed to
    the provider rather than trusting an opaque real model's output."""

    name = "echo"

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        return AssistantReply(text=request.system_context, provider=self.name)


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "grounding_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


async def test_assistant_router_passes_retrieved_source_ids_to_provider(conn):
    await fts.upsert(
        conn,
        source="vault",
        source_id="strategies/breakout.md",
        title="Breakout strategy notes",
        body="Wait for a confirmed close above resistance before entry.",
    )

    class _StubMemoryService:
        async def search(self, query, limit=3):
            return await fts.search(conn, query=query, limit=limit)

        async def index_conversation_message(self, message):
            pass

    router = AssistantRouter(
        event_service=EventService(conn),
        conversation_store=ConversationStore(conn),
        provider=_EchoProvider(),
        memory_service=_StubMemoryService(),
    )

    # The FTS query is an AND of every token, so it must use only words
    # actually present in the indexed title/body above.
    reply = await router.handle_text("breakout strategy notes", None)

    # The provider actually received the retrieved note's source_id — proof
    # that source traceability survives from FTS retrieval into grounding,
    # not just that *a* memory note was mentioned.
    assert "strategies/breakout.md" in reply.assistant_message.content


async def test_assistant_router_discloses_absence_when_nothing_is_retrieved(conn):
    class _EmptyMemoryService:
        async def search(self, query, limit=3):
            return []

        async def index_conversation_message(self, message):
            pass

    router = AssistantRouter(
        event_service=EventService(conn),
        conversation_store=ConversationStore(conn),
        provider=_EchoProvider(),
        memory_service=_EmptyMemoryService(),
    )

    reply = await router.handle_text(
        "What is the Sharpe ratio and win rate for XYZABC?", None
    )

    content = reply.assistant_message.content
    assert "don't have that information" in content
    assert "retrieved_memory_notes" not in content
