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

from assistant.errors import AssistantProviderError
from assistant.provider import AssistantProvider, AssistantReply, AssistantRequest


class ClaudeCodeProvider(AssistantProvider):
    name = "claude_code"

    def __init__(self, command: str = "claude", timeout_seconds: float = 60.0):
        self._command = command
        self._timeout = timeout_seconds

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        args = [self._command, "-p", request.text, "--output-format", "json"]
        if request.system_context:
            args += ["--append-system-prompt", request.system_context]

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
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

        if process.returncode != 0:
            raise AssistantProviderError(
                f"Claude Code CLI exited {process.returncode}: "
                f"{stderr.decode(errors='replace')[:500]}"
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
        return AssistantReply(text=result, provider=self.name)
