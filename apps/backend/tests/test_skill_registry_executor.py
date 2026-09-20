from __future__ import annotations

import asyncio
import json

from skill_registry.executor import (
    MAX_INSTRUCTION_CHARS,
    build_skill_execution_payload,
    execute_skill_prompt,
)

# ---- payload construction -------------------------------------------------

def test_payload_separates_instructions_task_and_constraints():
    system_context, stdin_text = build_skill_execution_payload("Debug REST APIs.", "review a PR")
    assert "Debug REST APIs." in stdin_text
    assert "review a PR" in stdin_text
    assert "<reference_material>" in stdin_text
    assert "<user_request>" in stdin_text
    # Constraints live in the separate system_context, not mixed into stdin.
    assert "no additional permissions" in system_context
    assert "cannot execute files, run shell commands" in system_context


def test_payload_never_uses_installed_skill_phrasing_that_collides_with_claude_code():
    # Regression: "Using the installed skill `X`. Its instructions: ..."
    # was pattern-matched by Claude Code's own native skill system,
    # observed directly to produce an empty/wrong response.
    system_context, stdin_text = build_skill_execution_payload("content", "task")
    combined = (system_context + stdin_text).lower()
    assert "installed skill" not in combined
    assert "using the installed" not in combined


def test_payload_caps_pathologically_large_content_as_a_safety_bound():
    huge = "x" * (MAX_INSTRUCTION_CHARS + 10_000)
    _, stdin_text = build_skill_execution_payload(huge, "task")
    assert len(stdin_text) < len(huge)
    assert "truncated" in stdin_text


def test_payload_does_not_truncate_realistic_skill_sizes():
    realistic = "# Skill\n" + ("Some instruction. " * 800)  # ~15KB, matches real-world SKILL.md
    assert len(realistic) < MAX_INSTRUCTION_CHARS
    _, stdin_text = build_skill_execution_payload(realistic, "task")
    assert "truncated" not in stdin_text
    assert realistic in stdin_text


# ---- execute_skill_prompt with a mocked subprocess -------------------------

class _FakeStdin:
    def __init__(self):
        self.written = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeStdout:
    def __init__(self, lines: list[bytes]):
        self._lines = lines
        self._idx = 0

    async def readline(self) -> bytes:
        if self._idx < len(self._lines):
            line = self._lines[self._idx]
            self._idx += 1
            return line
        return b""


class _FakeStderr:
    def __init__(self, data: bytes = b""):
        self._data = data

    async def read(self) -> bytes:
        return self._data


class _FakeProcess:
    def __init__(self, lines: list[bytes], returncode: int = 0, stderr: bytes = b""):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(lines)
        self.stderr = _FakeStderr(stderr)
        self.returncode = returncode
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    async def wait(self):
        return self.returncode


def _assistant_line(text: str) -> bytes:
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}).encode() + b"\n"


def _result_line(text: str) -> bytes:
    return json.dumps({"type": "result", "result": text}).encode() + b"\n"


async def test_execute_skill_prompt_succeeds_on_first_try(monkeypatch):
    calls: list[list[str]] = []

    async def fake_exec(*args, **kwargs):
        calls.append(list(args))
        return _FakeProcess([_assistant_line("A real answer."), _result_line("A real answer.")])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await execute_skill_prompt("skill content here", "do the task")
    assert result.success is True
    assert result.content == "A real answer."
    assert result.diagnostics.attempts == 1
    assert result.diagnostics.retried is False
    assert result.diagnostics.event_count == 2


async def test_execute_skill_prompt_never_puts_skill_content_in_argv(monkeypatch):
    captured_args: list[str] = []

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        return _FakeProcess([_result_line("ok")])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    huge_marker = "UNIQUE_MARKER_" + ("z" * 20000)
    await execute_skill_prompt(huge_marker, "task")

    for arg in captured_args:
        assert huge_marker not in arg, "skill content leaked into a CLI argv element"


async def test_execute_skill_prompt_sends_content_via_stdin(monkeypatch):
    procs: list[_FakeProcess] = []

    async def fake_exec(*args, **kwargs):
        proc = _FakeProcess([_result_line("ok")])
        procs.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await execute_skill_prompt("distinctive skill body", "distinctive task text")
    stdin_payload = procs[0].stdin.written.decode()
    assert "distinctive skill body" in stdin_payload
    assert "distinctive task text" in stdin_payload
    assert procs[0].stdin.closed is True


async def test_execute_skill_prompt_disables_native_tools(monkeypatch):
    captured_args: list[str] = []

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        return _FakeProcess([_result_line("ok")])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await execute_skill_prompt("content", "task")
    assert "--tools" in captured_args
    idx = captured_args.index("--tools")
    assert captured_args[idx + 1] == ""


async def test_execute_skill_prompt_retries_once_on_transient_empty_output(monkeypatch):
    attempt_count = 0

    async def fake_exec(*args, **kwargs):
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            return _FakeProcess([], returncode=0)  # exits 0, no events, no text -- transient
        return _FakeProcess([_result_line("Recovered on retry.")], returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await execute_skill_prompt("content", "task")
    assert result.success is True
    assert result.content == "Recovered on retry."
    assert result.diagnostics.attempts == 2
    assert result.diagnostics.retried is True


async def test_execute_skill_prompt_retries_at_most_once(monkeypatch):
    attempt_count = 0

    async def fake_exec(*args, **kwargs):
        nonlocal attempt_count
        attempt_count += 1
        return _FakeProcess([], returncode=0)  # always empty

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await execute_skill_prompt("content", "task")
    assert result.success is False
    assert attempt_count == 2  # exactly one retry, not more
    assert result.diagnostics.attempts == 2
    assert "empty output" in result.error


async def test_execute_skill_prompt_does_not_retry_on_genuine_error(monkeypatch):
    attempt_count = 0

    async def fake_exec(*args, **kwargs):
        nonlocal attempt_count
        attempt_count += 1
        return _FakeProcess([], returncode=1, stderr=b"permission denied")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await execute_skill_prompt("content", "task")
    assert result.success is False
    assert attempt_count == 1  # a real error is not retried
    assert "permission denied" in result.error


async def test_execute_skill_prompt_reports_explicit_error_not_fake_success(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return _FakeProcess([], returncode=1, stderr=b"boom")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await execute_skill_prompt("content", "task")
    assert result.success is False
    assert result.content == ""
    assert result.error is not None


async def test_execute_skill_prompt_captures_full_diagnostics(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return _FakeProcess(
            [_assistant_line("partial"), _result_line("final answer text")],
            returncode=0,
            stderr=b"some warning",
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await execute_skill_prompt("content", "task")
    d = result.diagnostics
    assert d.returncode == 0
    assert d.event_count == 2
    assert d.final_content_length == len("final answer text")
    assert d.stdout_bytes > 0
    assert "some warning" in d.stderr_text
    assert d.duration_seconds >= 0
    # to_dict() must be JSON-serializable for ActionResult.data
    json.dumps(d.to_dict())


async def test_execute_skill_prompt_missing_cli_returns_explicit_error(monkeypatch):
    async def fake_exec(*args, **kwargs):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await execute_skill_prompt("content", "task", command="definitely-not-claude")
    assert result.success is False
    assert "not found" in result.error.lower()


async def test_execute_skill_prompt_handles_short_medium_and_large_skill_content(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return _FakeProcess([_result_line("handled")])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    short_content = "Be concise."
    medium_content = "Follow these steps.\n" * 200  # ~4KB
    large_content = "Detailed instruction line.\n" * 2000  # ~56KB, exceeds argv limits observed live

    for content in (short_content, medium_content, large_content):
        result = await execute_skill_prompt(content, "task")
        assert result.success is True
        assert result.content == "handled"
