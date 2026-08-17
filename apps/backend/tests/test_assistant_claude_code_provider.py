from __future__ import annotations

import json

import pytest

from assistant.errors import AssistantProviderError
from assistant.provider import AssistantRequest
from assistant.providers.claude_code import ClaudeCodeProvider


class _FakeProcess:
    def __init__(self, returncode: int, stdout: bytes, stderr: bytes = b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):  # pragma: no cover - not exercised by these tests
        pass

    async def wait(self):  # pragma: no cover
        pass


@pytest.fixture
def provider():
    return ClaudeCodeProvider(command="claude", timeout_seconds=5.0)


async def test_session_id_is_remembered_and_resumed(provider, monkeypatch):
    calls: list[list[str]] = []

    async def fake_exec(*args, **kwargs):
        calls.append(list(args))
        payload = {"result": f"reply {len(calls)}", "session_id": "sess-123"}
        return _FakeProcess(0, json.dumps(payload).encode())

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    first = await provider.respond(
        AssistantRequest(text="hello", conversation_id="conv-1")
    )
    assert first.text == "reply 1"
    assert "--resume" not in calls[0]

    second = await provider.respond(
        AssistantRequest(text="follow up", conversation_id="conv-1")
    )
    assert second.text == "reply 2"
    assert "--resume" in calls[1]
    assert calls[1][calls[1].index("--resume") + 1] == "sess-123"


async def test_stale_session_falls_back_to_fresh_call(provider, monkeypatch):
    provider._sessions["conv-1"] = "stale-session"
    calls: list[list[str]] = []

    async def fake_exec(*args, **kwargs):
        calls.append(list(args))
        if len(calls) == 1:
            return _FakeProcess(1, b"", b"session not found")
        payload = {"result": "fresh reply", "session_id": "new-session"}
        return _FakeProcess(0, json.dumps(payload).encode())

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    reply = await provider.respond(AssistantRequest(text="hi again", conversation_id="conv-1"))
    assert reply.text == "fresh reply"
    assert len(calls) == 2
    assert "--resume" in calls[0]
    assert "--resume" not in calls[1]
    assert provider._sessions["conv-1"] == "new-session"


async def test_different_conversations_never_share_a_session(provider, monkeypatch):
    async def fake_exec(*args, **kwargs):
        payload = {"result": "ok", "session_id": "sess-a"}
        return _FakeProcess(0, json.dumps(payload).encode())

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    await provider.respond(AssistantRequest(text="hi", conversation_id="conv-a"))
    assert "conv-b" not in provider._sessions


async def test_both_attempts_failing_raises(provider, monkeypatch):
    provider._sessions["conv-1"] = "stale-session"

    async def fake_exec(*args, **kwargs):
        return _FakeProcess(1, b"", b"boom")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    with pytest.raises(AssistantProviderError):
        await provider.respond(AssistantRequest(text="hi", conversation_id="conv-1"))
