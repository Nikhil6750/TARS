from __future__ import annotations

import aiosqlite
import pytest

from memory.service import MemoryService
from storage.migrator import run_migrations


@pytest.fixture
async def memory(tmp_path):
    db_path = tmp_path / "memory_notes_test.db"
    run_migrations(db_path)
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    service = MemoryService(conn, vault_path=str(tmp_path / "vault"), sqlite_vec_enabled=False)
    yield service
    await conn.close()


async def test_remember_is_searchable_and_carries_provenance(memory):
    note_id = await memory.remember(
        "The user prefers risk capped at 1% per trade.",
        actor="user",
        conversation_id="conv-1",
    )
    results = await memory.search("risk capped", source="explicit_memory")
    assert len(results) == 1
    assert results[0]["source_id"] == note_id

    note = await memory.get_note(note_id)
    assert note["actor"] == "user"
    assert note["conversation_id"] == "conv-1"
    assert note["kind"] == "explicit_memory"


async def test_save_trading_observation_is_listable_by_symbol(memory):
    await memory.save_trading_observation(
        "Gold broke through resistance at 2400.", symbol="XAUUSD", actor="agent:chart_analysis_agent"
    )
    await memory.save_trading_observation("Unrelated note.", symbol="ES", actor="user")

    notes = await memory.list_notes("trading_observation", symbol="XAUUSD")
    assert len(notes) == 1
    assert notes[0]["symbol"] == "XAUUSD"
    assert notes[0]["actor"] == "agent:chart_analysis_agent"


async def test_save_decision_and_forget(memory):
    note_id = await memory.save_decision("Dispatched trading.get_trading_context() via the orchestrator.")
    notes = await memory.list_notes("decision")
    assert any(n["note_id"] == note_id for n in notes)

    assert await memory.forget(note_id) is True
    assert await memory.forget(note_id) is False
    assert await memory.get_note(note_id) is None
    assert await memory.search("Dispatched", source="decision") == []


async def test_session_memory_tracks_recent_turns(memory):
    from uuid import uuid4

    from app.schemas import AssistantMessage, InputMode, MessageRole

    conversation_id = uuid4()
    message = AssistantMessage(
        conversation_id=conversation_id,
        role=MessageRole.user,
        content="what is my risk limit",
        input_mode=InputMode.text,
    )
    await memory.index_conversation_message(message)
    recent = memory.session.recent(str(conversation_id))
    assert len(recent) == 1
    assert recent[0].text == "what is my risk limit"
