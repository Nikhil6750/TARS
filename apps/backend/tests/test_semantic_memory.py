"""Behavioral memory acceptance tests: statements -> durable meaning -> answers."""
from __future__ import annotations

import asyncio
import json
import time
from uuid import uuid4

import aiosqlite
import pytest

from app.schemas import AssistantMessage, InputMode, MessageRole
from memory import fts, notes
from memory.service import MemoryService
from storage.migrator import run_migrations


@pytest.fixture
async def memory(tmp_path):
    path = tmp_path / "memory.db"
    run_migrations(path)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    service = MemoryService(conn, str(tmp_path / "vault"), False)
    yield service
    await conn.close()


@pytest.mark.parametrize("verb", ["built", "created", "developed", "made"])
@pytest.mark.parametrize("subject", ["you", "TARS", "yourself", "this assistant"])
async def test_creator_paraphrases_survive_different_question_wording(memory, verb, subject):
    saved = await memory.remember_fact(f"Remember that {subject} were {verb} by developer Nikhil.")
    assert saved["status"] == "SAVED"
    assert len(saved["facts"]) == 1
    fact = saved["facts"][0]
    assert (fact["subject"], fact["relation"], fact["object"]) == ("TARS", "developed_by", "Nikhil")
    for question in ["Who made you?", "Who created TARS?", "Who developed you?", "Who's your developer?"]:
        assert await memory.memory_response(question) == "I was developed by Nikhil."
        recalled = await memory.recall_memory(question)
        assert recalled[0]["id"] == fact["id"]


@pytest.mark.parametrize("statement", [
    "Remember that Nikhil developed you.",
    "Remember that you got built or developed by a developer called Nikhil.",
    "Keep in mind that Nikhil made this assistant.",
    "Your developer is Nikhil.",
    "From now on, remember that Nikhil created TARS.",
    "I built you. My name is Nikhil.",
    "My name is Nikhil. I created you.",
])
async def test_explicit_knowledge_variants(memory, statement):
    result = await memory.remember_fact(statement)
    assert result["status"] == "SAVED"
    assert await memory.memory_response("Who built you?") == "I was developed by Nikhil."


async def test_i_me_resolution_uses_explicit_known_user_name(memory):
    await memory.remember_fact("My name is Nikhil.")
    assert (await memory.remember_fact("Remember that you were developed by me."))["status"] == "SAVED"
    assert await memory.memory_response("Who made you?") == "I was developed by Nikhil."


async def test_unresolved_pronoun_is_not_inferred(memory):
    result = await memory.remember_fact("I built you.")
    assert result["status"] == "NEEDS_CLARIFICATION"
    assert await memory.recall_memory("Who built you?") == []


@pytest.mark.parametrize(("statement", "value"), [
    ("My default timeframe is 15 minutes.", "15m"),
    ("My preferred chart timeframe is 15 min.", "15m"),
    ("Remember that my default chart time frame is 15m.", "15m"),
    ("My default timeframe is 60 minutes.", "1h"),
    ("My default timeframe is 1 hour.", "1h"),
])
async def test_preferences_normalize_units_and_default_chart_lookup(memory, statement, value):
    saved = await memory.remember_fact(statement)
    assert saved["status"] == "SAVED"
    fact = (await memory.recall_memory("Open my default chart."))[0]
    assert fact["object"] == value
    assert fact["memory_type"] == "PREFERENCE"
    assert fact["relation"] == "default_timeframe"
    response = await memory.memory_response("Open my default chart.")
    assert value in response and "opened" not in response.lower()


@pytest.mark.parametrize("statement,alias", [
    ("Remember that when I say gold I mean XAUUSD.", "gold"),
    ("Call gold XAU.", "XAU"),
    ("Remember that when I say yellow metal I mean XAUUSD.", "yellow metal"),
])
async def test_user_alias_resolution(memory, statement, alias):
    saved = await memory.remember_fact(statement)
    assert saved["status"] == "SAVED"
    assert saved["facts"][0]["memory_type"] == "ALIAS"
    assert await memory.semantic.resolve_symbol(alias) == "XAUUSD"
    assert (await memory.recall_memory(f"Check {alias}."))[0]["object"] == "XAUUSD"


async def test_default_symbol_and_timeframe_are_retrieved_together(memory):
    await memory.remember_fact("My default timeframe is 15 minutes.")
    await memory.remember_fact("My default symbol is gold.")
    assert {f["object"] for f in await memory.recall_memory("Open my default chart.")} == {"15m", "XAUUSD"}


async def test_entity_mentions_do_not_change_developer_canonical_identity(memory):
    first = await memory.remember_fact("Remember that developer Nikhil built you.")
    repeated = await memory.remember_fact("Remember that Nikhil made you.")
    assert repeated["status"] == "UNCHANGED"
    assert repeated["facts"][0]["id"] == first["facts"][0]["id"]


async def test_conflict_requires_clarification_and_explicit_correction_keeps_history(memory):
    first = (await memory.remember_fact("Remember that Nikhil developed you.", conversation_id="one"))["facts"][0]
    conflict = await memory.remember_fact("Remember that Arun developed you.", conversation_id="two")
    assert conflict["status"] == "NEEDS_CLARIFICATION"
    assert "Nikhil" in conflict["reason"] and "Arun" in conflict["reason"]
    assert await memory.memory_response("Who made you?") == "I was developed by Nikhil."
    corrected = await memory.remember_fact("Actually, Arun developed you.", conversation_id="two")
    assert corrected["status"] == "UPDATED"
    second = corrected["facts"][0]
    assert second["supersedes"] == first["id"]
    assert (await memory.get_note(first["id"]))["superseded_by"] == second["id"]
    assert (await memory.get_note(first["id"]))["body"] == "Remember that Nikhil developed you."
    assert await memory.memory_response("Who built you?") == "I was developed by Arun."
    assert len(await memory.list_notes("explicit_memory")) == 1
    assert await memory.search("Nikhil", source="explicit_memory") == []


async def test_stale_update_cannot_overwrite_newer_user_correction(memory):
    original = (await memory.remember_fact("My default timeframe is 15m."))["facts"][0]
    await memory.remember_fact("Actually, my default timeframe is 1h.")
    stale = await memory.remember_fact("My default timeframe is 5m.", expected_id=original["id"])
    assert stale["status"] == "CONFLICT"
    assert (await memory.recall_memory("What is my default timeframe?"))[0]["object"] == "1h"


async def test_concurrent_conflicting_writes_never_leave_two_active_facts(memory):
    results = await asyncio.gather(*[
        memory.remember_fact(f"Remember that {name} built you.") for name in ("Nikhil", "Arun")
    ])
    assert sum(r["status"] == "SAVED" for r in results) == 1
    assert len(await memory.recall_memory("Who made you?")) == 1


async def test_conflicting_multi_fact_utterance_is_atomic(memory):
    await memory.remember_fact("Remember that Nikhil built you.")
    result = await memory.remember_fact("Remember that my name is Arun. Arun built you.")
    assert result["status"] == "NEEDS_CLARIFICATION"
    assert await memory.recall_memory("What's my name?") == []


async def test_provenance_source_sentence_is_local_and_not_default_model_context(memory):
    text = "Remember that you were developed by Nikhil."
    result = await memory.remember_fact(text, conversation_id="voice-a", channel="gemini_live")
    fact = result["facts"][0]
    assert fact["source"] == {"actor": "user", "channel": "gemini_live", "conversation_id": "voice-a",
                              "source_id": fact["id"], "basis": "explicit_user_statement"}
    assert fact["confidence"] == 1.0
    assert fact["created_at"] == fact["updated_at"]
    assert fact["scope"] == "persistent"
    assert fact["canonical_entities"]["subject"] == "TARS"
    assert (await memory.get_note(fact["id"]))["body"] == text
    assert text not in json.dumps(await memory.recall_memory("Who made you?"))


async def test_retrieval_after_connection_close_and_process_state_replacement(tmp_path):
    path = tmp_path / "restart.db"
    run_migrations(path)
    async with aiosqlite.connect(path) as conn:
        conn.row_factory = aiosqlite.Row
        service = MemoryService(conn, "", False)
        await service.remember_fact("Remember that Nikhil made you.")
        await service.remember_fact("My default timeframe is 15m.")
        await service.remember_fact("Call gold XAU.")
    assert run_migrations(path) == []
    async with aiosqlite.connect(path) as conn:
        conn.row_factory = aiosqlite.Row
        fresh = MemoryService(conn, "", False)
        assert await fresh.memory_response("Who created TARS?") == "I was developed by Nikhil."
        assert (await fresh.recall_memory("Open my default chart."))[0]["object"] == "15m"
        assert await fresh.semantic.resolve_symbol("XAU") == "XAUUSD"


@pytest.mark.parametrize("text", [
    "hello", "thanks", "Remember hello.", "Remember that okay.",
    "Remember that gold is trading at 2600.", "My default timeframe is 15m for now.",
    "Remember that maybe Nikhil developed you.", "Remember that Arun did not make you.",
    "Remember that if Nikhil made you, call me.", "Remember that he built you.",
    "Remember that Nikhil or Arun made you.", "Remember that Nikhil and Arun made you.",
    "Remember that you were developed by someone.", "Who developed you?",
    "Your developer is someone.", "Your developer is him.",
    "Remember that my password is test-only-value.",
    "Remember that my API key is test-only-value.",
    "My project is https://name:secret@example.invalid.",  # pragma: allowlist secret -- synthetic rejection fixture
])
async def test_irrelevant_temporary_uncertain_or_sensitive_text_does_not_become_knowledge(memory, text):
    result = await memory.remember_fact(text)
    assert result["status"] not in {"SAVED", "UPDATED", "UNCHANGED"}
    assert await memory.list_notes("explicit_memory") == []


@pytest.mark.parametrize("actor", ["assistant", "system", "agent:chart_analysis_agent", "gemini"])
async def test_assistant_guesses_cannot_be_promoted_to_facts(memory, actor):
    result = await memory.remember_fact("Remember that Nikhil built you.", actor=actor)
    assert result["status"] == "REJECTED"
    assert await memory.recall_memory("Who made you?") == []


async def test_every_transcript_is_not_a_persistent_fact(memory):
    for role in (MessageRole.user, MessageRole.assistant):
        await memory.index_conversation_message(AssistantMessage(
            conversation_id=uuid4(), role=role, input_mode=InputMode.voice,
            content="Remember that Nikhil made you.",
        ))
    assert await memory.recall_memory("Who made you?") == []


async def test_ephemeral_fact_is_session_scoped_not_durable(memory):
    await memory.remember_fact("My default timeframe is 15m.", scope="ephemeral", conversation_id="a")
    assert await memory.recall_memory("What's my default timeframe?") == []
    assert await memory.recall_memory("What's my default timeframe?", scope="ephemeral", conversation_id="b") == []
    assert (await memory.recall_memory("What's my default timeframe?", scope="ephemeral", conversation_id="a"))[0]["object"] == "15m"
    assert await memory.list_notes("explicit_memory") == []
    memory.session.clear("a")
    assert await memory.recall_memory("What's my default timeframe?", scope="ephemeral", conversation_id="a") == []


async def test_project_context_and_preferences_are_not_trading_evidence(memory):
    await memory.remember_fact("I work on TARS.")
    await memory.remember_fact("I prefer dark mode.")
    project = (await memory.recall_memory("What am I working on?"))[0]
    assert project["memory_type"] == "PROJECT_CONTEXT"
    assert (await memory.recall_memory("What do I prefer?"))[0]["object"] == "dark mode"
    assert await memory.recall_memory("What is my strategy win rate?") == []


async def test_forget_erases_revision_sources_and_invalidates_cached_retrieval(memory):
    first = await memory.remember_fact("Remember that Nikhil developed you.")
    await memory.search("developer", use_cache=True)
    second = await memory.remember_fact("Actually, Arun developed you.")
    assert (await memory.search("developer", use_cache=True))[0]["fact"]["object"] == "Arun"
    assert await memory.forget(second["facts"][0]["id"])
    assert await memory.get_note(first["facts"][0]["id"]) is None
    assert await memory.search("developer", use_cache=True) == []
    assert not await memory.forget(second["facts"][0]["id"])


async def test_retrieval_does_not_answer_question_about_another_entity(memory):
    await memory.remember_fact("Remember that Nikhil built you.")
    assert await memory.recall_memory("Who made Arun?") == []


async def test_recall_is_bounded_and_nearly_instantaneous(memory):
    for name in [f"metal{i}" for i in range(40)]:
        await memory.remember_fact(f"Remember that when I say {name} I mean XAUUSD.")
    await memory.remember_fact("Remember that Nikhil built you.")
    assert len(await memory.recall_memory("XAUUSD", limit=1000)) == 5
    assert len(json.dumps(await memory.recall_memory("Who made you?"))) < 1500
    started = time.perf_counter()
    for _ in range(50):
        assert await memory.memory_response("Who made you?") == "I was developed by Nikhil."
    assert (time.perf_counter() - started) / 50 < 0.1


@pytest.mark.parametrize("query", ["", "* OR *", "'; DROP TABLE memory_notes; --", "x" * 1001])
async def test_query_inputs_cannot_expand_into_a_database_dump(memory, query):
    await memory.remember_fact("Remember that Nikhil made you.")
    assert await memory.recall_memory(query) == []
    assert await memory.memory_response("Who made you?") == "I was developed by Nikhil."


async def test_existing_explicit_notes_are_promoted_once_with_original_provenance(memory):
    original = await notes.insert(memory._conn, kind="explicit_memory", actor="user",
                                  body="you were developed by Nikhil", conversation_id="old-chat")
    await fts.upsert(memory._conn, "explicit_memory", original, "Remembered", "you were developed by Nikhil")
    old = await memory.get_note(original)
    assert await memory.semantic.import_legacy() == 1
    assert await memory.semantic.import_legacy() == 0
    assert await memory.memory_response("Who made you?") == "I was developed by Nikhil."
    fact = (await memory.recall_memory("Who made you?"))[0]
    assert fact["source"]["legacy_note_id"] == original
    assert fact["source"]["original_created_at"] == old["created_at"]
    assert fact["source"]["conversation_id"] == "old-chat"
    assert (await memory.get_note(original))["superseded_by"] == fact["id"]
    assert len(await memory.search("Nikhil", source="explicit_memory")) == 1
    await memory.forget(fact["id"])
    assert await memory.get_note(original) is None
    assert await memory.recall_memory("Who made you?") == []


@pytest.mark.parametrize("question", [
    "Who were you developed by?", "By whom was TARS created?", "Do you remember who built you?",
    "Can you tell me who made you?", "What is your developer's name?",
])
async def test_indirect_and_passive_creator_questions(memory, question):
    await memory.remember_fact("Remember that Nikhil built you.")
    assert await memory.memory_response(question) == "I was developed by Nikhil."


async def test_legacy_transcripts_guesses_and_old_conflicts_do_not_override(memory):
    await memory.remember_fact("Remember that Nikhil made you.")
    await notes.insert(memory._conn, kind="explicit_memory", actor="user", body="Actually, Arun made you.")
    await notes.insert(memory._conn, kind="explicit_memory", actor="assistant", body="my default timeframe is 5m")
    await notes.insert(memory._conn, kind="decision", actor="user", body="my name is Arun")
    assert await memory.semantic.import_legacy() == 0
    assert await memory.memory_response("Who made you?") == "I was developed by Nikhil."
    assert await memory.recall_memory("What is my default timeframe?") == []
    assert await memory.recall_memory("What is my name?") == []


async def test_disagreeing_legacy_notes_are_not_arbitrarily_ranked_as_truth(memory):
    for creator in ("Nikhil", "Arun"):
        note_id = await notes.insert(memory._conn, kind="explicit_memory", actor="user",
                                     body=f"{creator} made you")
        await fts.upsert(memory._conn, "explicit_memory", note_id, "Remembered", f"{creator} made you")
    assert await memory.semantic.import_legacy() == 0
    assert await memory.recall_memory("Who made you?") == []
    assert await memory.search("Nikhil", source="explicit_memory") == []
    assert all(n["metadata"]["semantic_import_status"] == "conflict" for n in await memory.list_notes("explicit_memory"))


async def test_forgetting_original_legacy_id_erases_its_promoted_fact(memory):
    original = await notes.insert(memory._conn, kind="explicit_memory", actor="user", body="Nikhil built you")
    await memory.semantic.import_legacy()
    assert await memory.forget(original)
    assert await memory.get_note(original) is None
    assert await memory.recall_memory("Who made you?") == []


async def test_migration_from_pre_semantic_database_preserves_existing_records(tmp_path, monkeypatch):
    from storage import migrator

    baseline = tmp_path / "baseline-migrations"
    baseline.mkdir()
    original = migrator.MIGRATIONS_DIR
    for sql in original.glob("*.sql"):
        if sql.name < "0010":
            (baseline / sql.name).write_bytes(sql.read_bytes())
    path = tmp_path / "upgrade.db"
    monkeypatch.setattr(migrator, "MIGRATIONS_DIR", baseline)
    run_migrations(path)
    async with aiosqlite.connect(path) as conn:
        conn.row_factory = aiosqlite.Row
        identifier = await notes.insert(conn, kind="explicit_memory", actor="user", body="Nikhil built you")
    monkeypatch.setattr(migrator, "MIGRATIONS_DIR", original)
    assert run_migrations(path) == ["0010_semantic_memory.sql"]
    async with aiosqlite.connect(path) as conn:
        conn.row_factory = aiosqlite.Row
        service = MemoryService(conn, "", False)
        assert await service.semantic.import_legacy() == 1
        assert await service.memory_response("Who made you?") == "I was developed by Nikhil."
        assert (await service.get_note(identifier))["body"] == "Nikhil built you"


@pytest.mark.parametrize("text", [
    "My app is broken. Can you help debug it?", "Your audio stopped working.",
    "What is a password?", "Explain API key authentication.",
    "Explain this problem: " + "a long ordinary reasoning request " * 50,
])
async def test_unrelated_personal_phrases_and_long_reasoning_keep_their_existing_route(memory, text):
    assert await memory.memory_response(text) is None
    assert await memory.list_notes("explicit_memory") == []
