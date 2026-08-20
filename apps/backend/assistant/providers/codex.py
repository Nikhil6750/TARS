"""Codex CLI Provider adapter.

Invokes the `codex` CLI in non-interactive / headless mode (`codex exec --ephemeral --json`).
Reports strict availability / missing provider errors with zero silent fallback.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from datetime import UTC, datetime

from assistant.errors import AssistantProviderError
from assistant.provider import (
    AssistantProvider,
    AssistantReply,
    AssistantRequest,
    ProviderDiagnostics,
)


class CodexProvider(AssistantProvider):
    name = "codex"

    def __init__(self, command: str = "codex", timeout_seconds: float = 60.0):
        self._raw_command = command
        self._command = shutil.which(command)
        self._timeout = timeout_seconds

    @property
    def is_available(self) -> bool:
        return self._command is not None

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        started_at = datetime.now(UTC).isoformat()
        t0 = time.perf_counter()

        if not self.is_available:
            diagnostics = ProviderDiagnostics(
                provider_id=self.name,
                provider_executable=None,
                started_at=started_at,
                completed_at=datetime.now(UTC).isoformat(),
                latency_ms=0.0,
                exit_code=None,
                fallback_used=False,
                error="MISSING_PROVIDER: Codex CLI not found on PATH",
            )
            raise AssistantProviderError(
                f"Codex CLI ('{self._raw_command}') not found on PATH — "
                "install it or configure CODEX_COMMAND"
            )

        prompt = request.text
        if request.system_context:
            prompt = f"System Context:\n{request.system_context}\n\nTask:\n{prompt}"

        base_args = [
            self._command,
            "exec",
            "--ephemeral",
            "--json",
            prompt,
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *base_args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._timeout
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise AssistantProviderError(
                f"Codex CLI timed out after {self._timeout}s"
            ) from exc
        except Exception as exc:
            raise AssistantProviderError(f"Codex CLI execution failed: {exc}") from exc

        latency_ms = (time.perf_counter() - t0) * 1000.0
        completed_at = datetime.now(UTC).isoformat()

        if process.returncode != 0:
            err_msg = stderr.decode(errors="replace")[:500]
            raise AssistantProviderError(f"Codex CLI exited {process.returncode}: {err_msg}")

        agent_messages: list[str] = []
        thread_id: str | None = None
        model_name: str = "gpt-5.6-sol"

        for line in stdout.decode(errors="replace").splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
                if event.get("type") == "thread.started":
                    thread_id = event.get("thread_id")
                elif event.get("type") == "item.completed":
                    item = event.get("item", {})
                    if item.get("type") == "agent_message" and item.get("text"):
                        agent_messages.append(item["text"])
            except Exception:
                continue

        result_text = "\n\n".join(agent_messages).strip()
        if not result_text:
            # Fallback to plain stdout if no JSONL agent_message event was decoded
            result_text = stdout.decode(errors="replace").strip()

        diagnostics = ProviderDiagnostics(
            provider_id=self.name,
            provider_executable=self._command,
            model=model_name,
            request_id=thread_id,
            started_at=started_at,
            completed_at=completed_at,
            latency_ms=round(latency_ms, 2),
            exit_code=process.returncode,
            fallback_used=False,
            error=None,
        )
        return AssistantReply(text=result_text, provider=self.name, diagnostics=diagnostics)
