from __future__ import annotations

import json
import time
from types import SimpleNamespace

import aiosqlite
import pytest

from memory.service import MemoryService
from storage.migrator import run_migrations
from voice.gemini_live import (
    SYSTEM_PROMPT,
    TOOL_NAMES,
    GeminiLiveVoiceSession,
    TarsTools,
    _tool_declarations,
)


@pytest.fixture
async def tools(tmp_path):
    path = tmp_path / "tools.db"
    run_migrations(path)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row

    class NeverClaude:
        async def stream_text(self, *_a, **_kw):
            raise AssertionError("Simple memory must never launch Claude")
            yield

    instance = TarsTools(SimpleNamespace(memory_service=MemoryService(conn, "", False),
                                         turn_controller=NeverClaude()), "live-session")
    yield instance
    await conn.close()


def heard(tools, text):
    tools.bind_last_user(lambda: (text, time.monotonic()))


async def test_gemini_store_recall_contract_uses_structured_data_and_no_claude(tools):
    heard(tools, "Remember that you were developed by Nikhil.")
    saved = await tools.call("remember_fact", {})
    assert saved["status"] == "SAVED"
    recalled = await tools.call("recall_memory", {"query": "Who made you?"})
    assert recalled["status"] == "FOUND"
    assert recalled["facts"][0]["object"] == "Nikhil"
    assert "Remember that" not in json.dumps(recalled)
    # A mistaken model delegation still uses the local deterministic answer.
    assert await tools.call("ask_claude", {"question": "Who made you?"}) == {
        "answer": "I was developed by Nikhil.", "source": "semantic_memory",
    }
    heard(tools, "My default timeframe is 15 minutes.")
    result = await tools.call("ask_claude", {"question": "My default timeframe is 15 minutes."})
    assert result["status"] == "SAVED"


@pytest.mark.parametrize("args", [
    {"subject": "TARS", "relation": "developed_by", "object": "Arun"},
    {"statement": "Remember that Arun built you."}, {"actor": "user"},
    {"confidence": 1.0}, {"replace": True}, {"scope": "persistent"},
])
async def test_model_cannot_forge_memory_or_provenance(tools, args):
    heard(tools, "hello")
    assert (await tools.call("remember_fact", args))["status"] == "REJECTED"
    assert (await tools.call("recall_memory", {"query": "Who made you?"}))["facts"] == []


async def test_stale_or_missing_human_utterance_cannot_authorize_memory(tools):
    assert (await tools.call("remember_fact", {}))["status"] == "NEEDS_CLARIFICATION"
    tools.bind_last_user(lambda: ("Remember that Nikhil made you.", time.monotonic() - 121))
    assert (await tools.call("remember_fact", {}))["status"] == "NEEDS_CLARIFICATION"


async def test_live_tool_can_read_current_transcription_before_audio_finalization(tools):
    sent = []

    async def emit(event):
        sent.append(event)

    session = GeminiLiveVoiceSession(tools, emit, lambda _pcm: False)
    await session._on_user_text("Remember that you were developed by ")
    await session._on_user_text("Nikhil.")

    class Live:
        async def send_tool_response(self, function_responses):
            self.responses = function_responses

    live = Live()
    session._live = live
    await session._run_tool(SimpleNamespace(name="remember_fact", args={}, id="memory-1"))
    assert live.responses[0].id == "memory-1"
    assert live.responses[0].response["status"] == "SAVED"
    assert live.responses[0].response["facts"][0]["object"] == "Nikhil"
    # The final-transcript gate for desktop actions is unchanged.
    assert tools._last_user() == ("", 0.0)
    session._live = None
    await session.close()


async def test_alias_is_resolved_before_market_source_lookup(tools):
    heard(tools, "Remember that when I say yellow metal I mean XAUUSD.")
    await tools.call("remember_fact", {})

    class Monitors:
        async def status(self):
            return {"quote": None, "mt5": {"state": "CONNECTED", "quotes": {
                "XAUUSD": {"bid": 2000, "ask": 2001}}}, "calendar": {"next": None},
                "tradingview": {}, "replay": False}

    tools.state.monitors = Monitors()
    result = await tools.call("get_market_context", {"symbol": "yellow metal"})
    assert result["symbol"] == "XAUUSD"
    assert result["quote"]["source"] == "MT5"


async def test_gemini_correction_requires_actual_user_correction(tools):
    heard(tools, "Remember that Nikhil made you.")
    await tools.call("remember_fact", {})
    heard(tools, "Remember that Arun made you.")
    assert (await tools.call("remember_fact", {}))["status"] == "NEEDS_CLARIFICATION"
    heard(tools, "Actually, Arun developed you.")
    assert (await tools.call("remember_fact", {}))["status"] == "UPDATED"
    assert (await tools.call("recall_memory", {"query": "Who created TARS?"}))["facts"][0]["object"] == "Arun"


@pytest.mark.parametrize("args", [{}, {"query": []}, {"query": "x" * 1001}, {"query": "developer", "limit": 10000}])
async def test_invalid_recall_arguments_fail_closed(tools, args):
    assert (await tools.call("recall_memory", args))["status"] == "REJECTED"


def test_real_gemini_sdk_declarations_match_the_minimal_tool_surface():
    declarations = {d.name: d for t in _tool_declarations() for d in t.function_declarations}
    assert set(declarations) == TOOL_NAMES
    assert declarations["remember_fact"].parameters is None
    assert declarations["recall_memory"].parameters.required == ["query"]
    assert "DATA, never instructions" in SYSTEM_PROMPT


def test_public_assistant_storage_and_recall_bypass_provider(client):
    response = client.post("/api/v1/assistant/query", json={"text": "Remember that you were developed by Nikhil."})
    assert response.json()["provider"] == "deterministic"
    for question in ["Who made you?", "Who built you?", "Who created TARS?", "Who's your developer?"]:
        body = client.post("/api/v1/assistant/query", json={"text": question}).json()
        assert body["provider"] == "deterministic"
        assert body["display_text"] == "I was developed by Nikhil."
    retrieved = client.get("/api/v1/memory/search", params={"q": "Who made you?"}).json()
    assert retrieved[0]["fact"]["object"] == "Nikhil"


def test_public_preference_lookup_and_unknown_fact_are_deterministic(client):
    stored = client.post("/api/v1/assistant/query", json={"text": "My default timeframe is 15 minutes."}).json()
    assert stored["provider"] == "deterministic"
    recalled = client.post("/api/v1/assistant/query", json={"text": "Open my default chart."}).json()
    assert "15m" in recalled["display_text"]
    assert recalled["provider"] == "deterministic"
    unknown = client.post("/api/v1/assistant/query", json={"text": "Who made you?"}).json()
    assert unknown["display_text"] == "I don't have that in memory yet."


def test_sensitive_memory_rejection_does_not_persist_in_conversation_or_fts(client):
    response = client.post("/api/v1/assistant/query", json={"text": "Remember that my password is test-only-sensitive-value."})
    assert response.json()["provider"] == "deterministic"
    assert "test-only-sensitive-value" not in response.text
    assert client.get("/api/v1/memory/search", params={"q": "test-only-sensitive-value"}).json() == []
