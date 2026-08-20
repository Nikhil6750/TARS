"""Rides the user's own authenticated Claude Code environment — a
high-intelligence option for personal installs, distinct from calling the
Anthropic API directly (see AnthropicAPIProvider). Invokes the `claude` CLI
in headless/"print mode" (`claude -p ... --output-format json`), which
returns a single structured response and exits rather than opening the
interactive REPL.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from assistant.errors import AssistantProviderError
from assistant.provider import (
    AssistantProvider,
    AssistantReply,
    AssistantRequest,
    ProviderDiagnostics,
)

# asyncio.create_subprocess_exec's stdout defaults to a 64KB StreamReader
# line-buffer limit. Chart analysis's stream-json output includes a
# `tool_result` event that echoes the captured image back to Claude as a
# single base64-encoded JSON line -- routinely several hundred KB, well
# over that default -- which makes process.stdout.readline() in
# respond_stream() hang indefinitely rather than raise (observed: a
# ~680KB line against the 64KB default). Plain chat requests never emit a
# line anywhere near this size; only image-carrying events do.
CLAUDE_STREAM_READER_LIMIT_BYTES = 32 * 1024 * 1024

_TOOL_LEAKAGE_PATTERN = re.compile(
    r"(my\s+)?(web\s*search\s+tool|search\s+tool|tool)\s+was\s+blocked[^.]*\.?"
    r"|permission\s+prompt\s+wasn't\s+granted[^.]*\.?"
    r"|claude\s+permission\s+wasn't\s+granted[^.]*\.?"
    r"|websearch\s+permission[^.]*\.?"
    r"|cli\s+tool\s+unavailable[^.]*\.?",
    re.IGNORECASE,
)


def sanitize_user_facing_text(text: str) -> str:
    """Removes CLI tool blockages and permission denial leakage from user-facing text."""
    cleaned = _TOOL_LEAKAGE_PATTERN.sub("", text).strip()
    # Clean up any leftover leading dashes/punctuation from removed prefix
    cleaned = re.sub(r"^[\s—\-\:\,]+", "", cleaned).strip()
    return cleaned


class ClaudeCodeProvider(AssistantProvider):
    name = "claude_code"

    def __init__(self, command: str = "claude", timeout_seconds: float = 60.0):
        self._raw_command = command
        self._command = shutil.which(command) or command
        self._timeout = timeout_seconds
        self._sessions: dict[str, str] = {}

    def _resume_args(self, conversation_id: str) -> list[str]:
        session_id = self._sessions.get(conversation_id)
        return ["--resume", session_id] if session_id else []

    def _remember_session(self, conversation_id: str, session_id: str | None) -> None:
        if session_id:
            self._sessions[conversation_id] = session_id

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        started_at = datetime.now(UTC).isoformat()
        t0 = time.perf_counter()
        prompt = request.text
        allowed_tools: list[str] = []
        extra_dir: str | None = None
        if request.image_path:
            prompt = f"Use the Read tool to open the image file at {request.image_path}, then: {prompt}"
            allowed_tools = ["Read"]
            extra_dir = str(Path(request.image_path).parent)

        base_args = [self._command, "-p", prompt, "--output-format", "json"]
        system_context = request.system_context or ""
        # Instruction to prevent ungrounded tool searching
        system_context += "\nDo not invoke external search tools or invent current unretrieved facts."
        base_args += ["--append-system-prompt", system_context]
        if allowed_tools:
            base_args += ["--allowedTools", *allowed_tools]
        if extra_dir:
            base_args += ["--add-dir", extra_dir]

        resume_args = self._resume_args(request.conversation_id)
        returncode, stdout, stderr = await self._run_cli(base_args + resume_args)
        if returncode != 0 and resume_args:
            self._sessions.pop(request.conversation_id, None)
            returncode, stdout, stderr = await self._run_cli(base_args)

        latency_ms = (time.perf_counter() - t0) * 1000.0
        completed_at = datetime.now(UTC).isoformat()

        if returncode != 0:
            err_msg = stderr.decode(errors="replace")[:500]
            raise AssistantProviderError(
                f"Claude Code CLI exited {returncode}: {err_msg}"
            )

        try:
            payload = json.loads(stdout.decode(errors="replace"))
        except json.JSONDecodeError as exc:
            raise AssistantProviderError(
                "Claude Code CLI did not return valid JSON output"
            ) from exc

        result = payload.get("result")
        if not result:
            raise AssistantProviderError(
                "Claude Code CLI JSON output had no 'result' field"
            )
        self._remember_session(request.conversation_id, payload.get("session_id"))

        # Extract model if available
        model_name = None
        if isinstance(payload.get("modelUsage"), dict):
            models = list(payload["modelUsage"].keys())
            if models:
                model_name = models[-1]

        sanitized_text = sanitize_user_facing_text(result)
        diagnostics = ProviderDiagnostics(
            provider_id=self.name,
            provider_executable=self._command,
            model=model_name or "claude-code",
            request_id=payload.get("uuid") or payload.get("session_id"),
            started_at=started_at,
            completed_at=completed_at,
            latency_ms=round(latency_ms, 2),
            exit_code=returncode,
            fallback_used=False,
            error=None,
        )
        return AssistantReply(text=sanitized_text, provider=self.name, diagnostics=diagnostics)

    async def _run_cli(self, args: list[str]) -> tuple[int, bytes, bytes]:
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise AssistantProviderError(
                f"Claude Code CLI ('{self._command}') not found on PATH — "
                "install it or set CLAUDE_CODE_COMMAND to its full path"
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._timeout
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise AssistantProviderError(
                f"Claude Code CLI timed out after {self._timeout}s"
            ) from exc
        return process.returncode or 0, stdout, stderr

    async def respond_stream(self, request: AssistantRequest):
        prompt = request.text
        allowed_tools: list[str] = []
        extra_dir: str | None = None
        if request.image_path:
            prompt = f"Use the Read tool to open the image file at {request.image_path}, then: {prompt}"
            allowed_tools = ["Read"]
            extra_dir = str(Path(request.image_path).parent)

        args = [self._command, "-p", prompt, "--output-format", "stream-json", "--verbose"]
        system_context = request.system_context or ""
        system_context += "\nDo not invoke external search tools or invent current unretrieved facts."
        args += ["--append-system-prompt", system_context]
        if allowed_tools:
            args += ["--allowedTools", *allowed_tools]
        if extra_dir:
            args += ["--add-dir", extra_dir]
        # Best-effort session reuse only here (no stale-session retry, unlike
        # respond() -- streaming may have already yielded partial content by
        # the time a failure surfaces, so silently restarting isn't safe).
        args += self._resume_args(request.conversation_id)
        session_id: str | None = None

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=CLAUDE_STREAM_READER_LIMIT_BYTES,
            )
        except FileNotFoundError as exc:
            raise AssistantProviderError(
                f"Claude Code CLI ('{self._command}') not found on PATH — "
                "install it or set CLAUDE_CODE_COMMAND to its full path"
            ) from exc

        accumulated_text = ""
        emitted_length = 0
        final_result_text = ""
        image_confirmed_read = False

        try:
            while True:
                try:
                    line = await asyncio.wait_for(process.stdout.readline(), timeout=self._timeout)
                except TimeoutError as exc:
                    raise AssistantProviderError(
                        f"Claude Code CLI produced no output for {self._timeout}s and was killed"
                    ) from exc
                if not line:
                    break
                decoded = line.decode(errors="replace").strip()
                if not decoded or not decoded.startswith("{"):
                    continue
                try:
                    event = json.loads(decoded)
                except json.JSONDecodeError:
                    continue

                if isinstance(event.get("session_id"), str) and event["session_id"]:
                    session_id = event["session_id"]

                event_type = event.get("type")
                if event_type == "user" and not image_confirmed_read:
                    content_list = event.get("message", {}).get("content", [])
                    if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content_list):
                        image_confirmed_read = True
                        yield {"type": "status", "text": "Reading the chart..."}
                if event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    text_chunk = delta.get("text", "")
                    if text_chunk:
                        accumulated_text += text_chunk
                        emitted_length += len(text_chunk)
                        yield {"type": "delta", "text": text_chunk}
                elif event_type == "assistant":
                    msg = event.get("message", {})
                    content_list = msg.get("content", [])
                    curr_full = "".join(c.get("text", "") for c in content_list if isinstance(c, dict))
                    if len(curr_full) > emitted_length:
                        new_piece = curr_full[emitted_length:]
                        emitted_length = len(curr_full)
                        accumulated_text = curr_full
                        yield {"type": "delta", "text": new_piece}
                elif event_type == "result":
                    final_result_text = event.get("result", "")
                    if final_result_text and len(final_result_text) > emitted_length:
                        new_piece = final_result_text[emitted_length:]
                        emitted_length = len(final_result_text)
                        accumulated_text = final_result_text
                        yield {"type": "delta", "text": new_piece}

            await process.wait()
            if process.returncode != 0:
                stderr_bytes = await process.stderr.read()
                err_text = stderr_bytes.decode(errors="replace")
                if not accumulated_text:
                    raise AssistantProviderError(f"Claude Code stream exited {process.returncode}: {err_text[:500]}")

            self._remember_session(request.conversation_id, session_id)
            final_text = final_result_text or accumulated_text
            final_text = sanitize_user_facing_text(final_text)
            yield {"type": "complete", "text": final_text, "provider": self.name}
        except Exception:
            process.kill()
            await process.wait()
            raise

