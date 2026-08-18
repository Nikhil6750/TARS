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
from pathlib import Path

from assistant.errors import AssistantProviderError
from assistant.provider import AssistantProvider, AssistantReply, AssistantRequest

# asyncio.create_subprocess_exec's stdout defaults to a 64KB StreamReader
# line-buffer limit. Chart analysis's stream-json output includes a
# `tool_result` event that echoes the captured image back to Claude as a
# single base64-encoded JSON line -- routinely several hundred KB, well
# over that default -- which makes process.stdout.readline() in
# respond_stream() hang indefinitely rather than raise (observed: a
# ~680KB line against the 64KB default). Plain chat requests never emit a
# line anywhere near this size; only image-carrying events do.
CLAUDE_STREAM_READER_LIMIT_BYTES = 32 * 1024 * 1024


class ClaudeCodeProvider(AssistantProvider):
    name = "claude_code"

    def __init__(self, command: str = "claude", timeout_seconds: float = 60.0):
        self._raw_command = command
        self._command = shutil.which(command) or command
        self._timeout = timeout_seconds
        # conversation_id -> claude CLI session_id, process-local only (see
        # TARS core § Performance: "reusable Claude session/context"). Lets
        # the CLI's own session carry prior turns instead of this provider
        # reconstructing history itself (it doesn't touch
        # AssistantRequest.history -- the deterministic system_context is
        # still sent fresh every call, since grounding facts can change
        # turn to turn even within one resumed session). Never persisted;
        # a process restart just starts fresh sessions, which is fine.
        self._sessions: dict[str, str] = {}

    def _resume_args(self, conversation_id: str) -> list[str]:
        session_id = self._sessions.get(conversation_id)
        return ["--resume", session_id] if session_id else []

    def _remember_session(self, conversation_id: str, session_id: str | None) -> None:
        if session_id:
            self._sessions[conversation_id] = session_id

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        prompt = request.text
        allowed_tools: list[str] = []
        extra_dir: str | None = None
        if request.image_path:
            # Claude Code's own Read tool opens local image files given a
            # path -- no separate multimodal API call needed. Three things
            # are required for this to actually happen rather than Claude
            # replying that nothing was attached or that the read was
            # blocked: (1) an explicit instruction to use the Read tool
            # (naming the path alone is not read as an instruction to open
            # it), (2) allow-listing exactly the Read tool, since a
            # headless `-p` run has no TTY to approve a tool call
            # interactively and it is otherwise silently skipped, and (3)
            # `--add-dir` naming the image's own temp directory, since
            # Claude Code scopes file access to the invocation's working
            # directory by default and the image lives outside it. All
            # three are scoped to exactly one read-only tool, against
            # exactly the one directory holding the one file this backend
            # itself just wrote (see assistant/chart_analysis.py) -- never
            # write/execute access, and never an arbitrary caller-supplied
            # path or directory.
            prompt = f"Use the Read tool to open the image file at {request.image_path}, then: {prompt}"
            allowed_tools = ["Read"]
            extra_dir = str(Path(request.image_path).parent)

        base_args = [self._command, "-p", prompt, "--output-format", "json"]
        if request.system_context:
            base_args += ["--append-system-prompt", request.system_context]
        if allowed_tools:
            base_args += ["--allowedTools", *allowed_tools]
        if extra_dir:
            base_args += ["--add-dir", extra_dir]

        resume_args = self._resume_args(request.conversation_id)
        returncode, stdout, stderr = await self._run_cli(base_args + resume_args)
        if returncode != 0 and resume_args:
            # A cached session_id can go stale (CLI restarted, session
            # expired) -- fall back to a fresh session once rather than
            # failing the whole request over a perf optimization.
            self._sessions.pop(request.conversation_id, None)
            returncode, stdout, stderr = await self._run_cli(base_args)
        if returncode != 0:
            raise AssistantProviderError(
                f"Claude Code CLI exited {returncode}: {stderr.decode(errors='replace')[:500]}"
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
        return AssistantReply(text=result, provider=self.name)

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
        if request.system_context:
            args += ["--append-system-prompt", request.system_context]
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
        last_emitted_len = 0
        final_result_text = ""

        try:
            while True:
                # Per-line timeout, not one overall deadline: a slow model
                # producing steady output shouldn't be killed just because
                # the whole reply took a while, but the CLI going fully
                # silent for this long (a stuck tool-approval prompt with no
                # TTY to answer it, a hung network call, etc.) means it will
                # never finish on its own -- respond()'s single-call
                # asyncio.wait_for can't help here since this loop reads
                # incrementally instead of blocking on one process.communicate().
                try:
                    line = await asyncio.wait_for(process.stdout.readline(), timeout=self._timeout)
                except TimeoutError as exc:
                    # Cleanup (kill + wait) happens once, in the `except
                    # Exception` block below -- doing it here too would
                    # double-kill an already-reaped process.
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
                if event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    text_chunk = delta.get("text", "")
                    if text_chunk:
                        accumulated_text += text_chunk
                        yield {"type": "delta", "text": text_chunk}
                elif event_type == "assistant":
                    msg = event.get("message", {})
                    content_list = msg.get("content", [])
                    curr_full = "".join(c.get("text", "") for c in content_list if isinstance(c, dict))
                    if len(curr_full) > last_emitted_len:
                        new_piece = curr_full[last_emitted_len:]
                        last_emitted_len = len(curr_full)
                        accumulated_text = curr_full
                        yield {"type": "delta", "text": new_piece}
                elif event_type == "result":
                    final_result_text = event.get("result", "")
                    if final_result_text and len(final_result_text) > len(accumulated_text):
                        new_piece = final_result_text[len(accumulated_text):]
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
            yield {"type": "complete", "text": final_text, "provider": self.name}
        except Exception:
            process.kill()
            await process.wait()
            raise
