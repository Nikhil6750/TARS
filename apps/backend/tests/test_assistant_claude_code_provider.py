from __future__ import annotations

import asyncio
import json
import sys

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


class _FakeStdout:
    """Feeds queued lines one at a time; `hang=True` makes the read after
    the last queued line block forever instead of returning EOF, simulating
    a Claude Code CLI subprocess that stops producing output entirely (e.g.
    stuck on a tool-approval prompt with no TTY to answer it)."""

    def __init__(self, lines: list[bytes], hang: bool = False):
        self._lines = lines
        self._idx = 0
        self._hang = hang

    async def readline(self) -> bytes:
        if self._idx < len(self._lines):
            line = self._lines[self._idx]
            self._idx += 1
            return line
        if self._hang:
            await asyncio.sleep(3600)
        return b""


class _FakeStderr:
    def __init__(self, data: bytes = b""):
        self._data = data

    async def read(self) -> bytes:
        return self._data


class _FakeStreamProcess:
    def __init__(self, lines: list[bytes], returncode: int = 0, hang: bool = False, stderr: bytes = b""):
        self.stdout = _FakeStdout(lines, hang=hang)
        self.stderr = _FakeStderr(stderr)
        self.returncode = returncode
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    async def wait(self):
        return self.returncode


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


async def test_respond_stream_yields_deltas_and_completes(provider, monkeypatch):
    lines = [
        json.dumps({"type": "assistant", "message": {"content": [{"text": "Hello"}]}}).encode() + b"\n",
        json.dumps({"type": "result", "result": "Hello world", "session_id": "sess-xyz"}).encode() + b"\n",
    ]

    async def fake_exec(*args, **kwargs):
        return _FakeStreamProcess(lines)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    events = [event async for event in provider.respond_stream(AssistantRequest(text="hi", conversation_id="conv-1"))]

    deltas = [e["text"] for e in events if e["type"] == "delta"]
    assert deltas == ["Hello", " world"]
    complete = next(e for e in events if e["type"] == "complete")
    assert complete["text"] == "Hello world"
    assert provider._sessions["conv-1"] == "sess-xyz"


async def test_respond_stream_kills_and_raises_when_cli_goes_silent(monkeypatch):
    # A CLI that emits nothing at all (e.g. stuck on a tool-approval prompt
    # with no TTY to answer it -- see claude_code.py's respond_stream
    # comment) must be killed and reported, not hang forever. Regression
    # test for the missing timeout that let this happen in practice.
    provider = ClaudeCodeProvider(command="claude", timeout_seconds=0.05)
    fake_process = _FakeStreamProcess([], hang=True)

    async def fake_exec(*args, **kwargs):
        return fake_process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    with pytest.raises(AssistantProviderError, match="no output"):
        async for _ in provider.respond_stream(AssistantRequest(text="hi", conversation_id="conv-1")):
            pass

    assert fake_process.killed is True


async def test_respond_stream_does_not_time_out_on_steady_slow_output(monkeypatch):
    # A per-line timeout must not fire just because the whole reply takes a
    # while, as long as new lines keep arriving within the timeout window.
    provider = ClaudeCodeProvider(command="claude", timeout_seconds=0.2)

    class _SlowStdout:
        def __init__(self):
            self._sent = 0

        async def readline(self) -> bytes:
            if self._sent == 0:
                await asyncio.sleep(0.05)
                self._sent += 1
                return json.dumps({"type": "assistant", "message": {"content": [{"text": "slow"}]}}).encode() + b"\n"
            if self._sent == 1:
                await asyncio.sleep(0.05)
                self._sent += 1
                return json.dumps({"type": "result", "result": "slow but steady", "session_id": "s1"}).encode() + b"\n"
            return b""

    class _SlowProcess(_FakeStreamProcess):
        def __init__(self):
            super().__init__([])
            self.stdout = _SlowStdout()

    async def fake_exec(*args, **kwargs):
        return _SlowProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    events = [event async for event in provider.respond_stream(AssistantRequest(text="hi", conversation_id="conv-1"))]
    complete = next(e for e in events if e["type"] == "complete")
    assert complete["text"] == "slow but steady"


async def test_respond_stream_handles_line_far_larger_than_default_asyncio_limit(monkeypatch):
    """Regression test for the real production bug: asyncio's default
    StreamReader line limit is 64KB, but chart analysis's `tool_result`
    stream-json event (the captured screenshot echoed back to Claude as
    base64) is routinely several hundred KB on a single line -- observed
    ~680KB in production, which made process.stdout.readline() hang
    indefinitely on Windows rather than raise. Spawns a REAL child process
    and reads it through the REAL asyncio subprocess/StreamReader machinery
    (only the program being run is faked, not readline() itself), so this
    actually proves the `limit=CLAUDE_STREAM_READER_LIMIT_BYTES` fix works
    end to end -- not just that our own JSON dispatch logic is fine.
    """
    # The huge payload must be GENERATED by the script at runtime, not
    # embedded as a literal in the script text itself -- the script text is
    # a command-line argument, and Windows' CreateProcess has its own
    # ~32KB command-line length limit, unrelated to the StreamReader limit
    # actually under test here.
    fake_cli_script = (
        "import json\n"
        "print(json.dumps({'type': 'user', 'padding': 'x' * (1024 * 1024)}), flush=True)\n"
        "print(json.dumps({'type': 'assistant', 'message': {'content': [{'text': 'ok after huge line'}]}}), flush=True)\n"
        "print(json.dumps({'type': 'result', 'result': 'ok after huge line', 'session_id': 'sess-huge'}), flush=True)\n"
    )

    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def fake_exec(*args, **kwargs):
        # Ignores the real CLI args entirely and runs our controlled script
        # instead -- but forwards `limit` exactly as the provider passed it,
        # since that kwarg is the actual thing under test.
        return await real_create_subprocess_exec(
            sys.executable,
            "-c",
            fake_cli_script,
            stdin=kwargs.get("stdin"),
            stdout=kwargs.get("stdout"),
            stderr=kwargs.get("stderr"),
            limit=kwargs.get("limit", 65536),
        )

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    provider = ClaudeCodeProvider(command="claude", timeout_seconds=10.0)
    events = await asyncio.wait_for(
        _collect(provider.respond_stream(AssistantRequest(text="hi", conversation_id="conv-1"))),
        timeout=15.0,
    )

    complete = next(e for e in events if e["type"] == "complete")
    assert complete["text"] == "ok after huge line"
    assert provider._sessions["conv-1"] == "sess-huge"


async def _collect(async_gen):
    return [event async for event in async_gen]
