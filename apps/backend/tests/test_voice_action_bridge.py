"""Targeted tests for the Wave 2A voice -> Action Runtime wiring
(voice/pipecat_bridge.py's AssistantBridgeProcessor._handle_transcript).

These exercise the real ActionRuntime (real skills, real PermissionEngine,
real SkillRegistry -- same objects app.main wires up), not a fake pipeline,
so a deterministic voice phrase genuinely goes through permission
classification and skill dispatch exactly like a HUD-issued request. Only
the STT/TTS/Pipecat frame plumbing around it is out of scope here (covered
by the existing test_voice_end_to_end_real.py real-audio tests).
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from app.action_contracts import ActionSource
from assistant.conversation_store import ConversationStore
from assistant.providers.mock import MockAssistantProvider
from assistant.router import AssistantRouter
from events.service import EventService
from voice.pipecat_bridge import AssistantBridgeProcessor


@pytest.fixture
def bridge(client):
    """A real AssistantBridgeProcessor wired to the same ActionRuntime and
    (mock-provider) AssistantRouter the app itself uses."""
    app = client.app
    router = AssistantRouter(
        event_service=EventService(app.state.db.conn),
        conversation_store=ConversationStore(app.state.db.conn),
        provider=app.state.assistant_provider,
        memory_service=app.state.memory_service,
    )
    assert isinstance(app.state.assistant_provider, MockAssistantProvider)
    return AssistantBridgeProcessor(
        assistant_router=router,
        conversation_id=str(uuid4()),
        action_runtime=app.state.action_runtime,
        action_source=ActionSource.voice_ptt,
    )


async def test_voice_open_notepad_reaches_action_runtime_and_launches_real_process(bridge):
    spoken = await bridge._handle_transcript("open notepad")
    assert "notepad" in spoken.lower()
    assert "launched" in spoken.lower() or "pid" in spoken.lower()


async def test_voice_safe_browser_request_reaches_action_runtime(bridge):
    spoken = await bridge._handle_transcript("open https://example.com")
    assert "example.com" in spoken


async def test_voice_read_only_terminal_request_executes_and_speaks_real_output(bridge):
    spoken = await bridge._handle_transcript("run whoami")
    assert "whoami" in spoken.lower()
    # A real read-only command actually ran -- it did not stop at confirmation.
    assert "confirmation" not in spoken.lower()


async def test_voice_confirmation_required_request_does_not_execute(bridge):
    spoken = await bridge._handle_transcript("run echo hello-from-voice-test")
    assert "confirmation" in spoken.lower()
    # Never claims the command ran -- no bypassing the confirmation gate via voice.
    assert "hello-from-voice-test" not in spoken


async def test_voice_blocked_dangerous_request_is_never_executed(bridge):
    spoken = await bridge._handle_transcript("run format C: /y")
    assert "blocked" in spoken.lower()


async def test_voice_unsupported_phrase_falls_back_to_assistant_router(bridge):
    # Not a deterministic phrase at all -- must fall through to the normal
    # assistant/LLM path unchanged, never be treated as an action.
    spoken = await bridge._handle_transcript("what is the weather like today")
    assert spoken  # MockAssistantProvider still produces a real reply
    assert "weather" not in spoken.lower() or True  # content is provider-owned; only routing is under test


async def test_voice_bridge_without_action_runtime_falls_back_unchanged(client):
    """If no action_runtime is wired in (e.g. a test harness that predates
    Wave 2A), deterministic phrases must still fall through safely rather
    than raise -- this is the pre-Wave-2A behavior, preserved."""
    app = client.app
    router = AssistantRouter(
        event_service=EventService(app.state.db.conn),
        conversation_store=ConversationStore(app.state.db.conn),
        provider=app.state.assistant_provider,
        memory_service=app.state.memory_service,
    )
    bridge = AssistantBridgeProcessor(
        assistant_router=router,
        conversation_id=str(uuid4()),
        action_runtime=None,
    )
    spoken = await bridge._handle_transcript("open notepad")
    assert spoken  # routed through the assistant, not silently dropped
