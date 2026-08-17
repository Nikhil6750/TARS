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
from pathlib import Path

from assistant.errors import AssistantProviderError
from assistant.provider import AssistantProvider, AssistantReply, AssistantRequest


class ClaudeCodeProvider(AssistantProvider):
    name = "claude_code"

    def __init__(self, command: str = "claude", timeout_seconds: float = 60.0):
        self._command = command
        self._timeout = timeout_seconds

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

        args = [self._command, "-p", prompt, "--output-format", "json"]
        if request.system_context:
            args += ["--append-system-prompt", request.system_context]
        if allowed_tools:
            args += ["--allowedTools", *allowed_tools]
        if extra_dir:
            args += ["--add-dir", extra_dir]

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
