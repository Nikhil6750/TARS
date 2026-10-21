"""Production Claude adapter for the ResearchDirector.

The director's `llm` is `async (system, user) -> str`; this class provides it by delegating to TARS's existing
AssistantProvider (ClaudeCodeProvider by default). Safeguards:
  * lazy: constructing the adapter never calls Claude; nothing happens at startup
  * explicit use only: one call per director operation, never per candle/tick
  * hard per-instance call budget, prompt-size cap and the cloud kill switch (settings.force_offline)
  * audit log of every call (hash + sizes + latency, NOT the content)"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .agent import QuantResearchAgent
from .director import ResearchDirector
from .registry import ExperimentRegistry

MAX_PROMPT_CHARS = 24000


class ClaudeUnavailable(RuntimeError):
    pass


class ClaudeResearchLLM:
    def __init__(self, provider=None, *, timeout_seconds: float = 240.0, max_calls: int = 12, log_path: Path | None = None,
                 cloud_disabled=lambda: False) -> None:
        self._provider = provider
        self.timeout_seconds, self.max_calls = timeout_seconds, max_calls
        self.log_path = Path(log_path) if log_path else None
        self.calls = 0
        self._cloud_disabled = cloud_disabled

    def _get_provider(self):
        if self._provider is None:
            from assistant.providers.claude_code import ClaudeCodeProvider
            self._provider = ClaudeCodeProvider(timeout_seconds=self.timeout_seconds, persist_sessions=False)
        return self._provider

    async def __call__(self, system: str, user: str) -> str:
        from assistant.provider import AssistantRequest
        if self._cloud_disabled():
            raise ClaudeUnavailable("cloud reasoning is disabled (application-level kill switch)")
        if self.calls >= self.max_calls:
            raise ClaudeUnavailable(f"research Claude call budget exhausted ({self.max_calls})")
        if len(system) + len(user) > MAX_PROMPT_CHARS:
            raise ClaudeUnavailable(f"prompt of {len(system) + len(user)} chars exceeds cap {MAX_PROMPT_CHARS}; send an evidence summary")
        self.calls += 1
        t0 = time.perf_counter()
        status = "ok"
        try:
            reply = await self._get_provider().respond(
                AssistantRequest(text=user, conversation_id=f"research-{uuid.uuid4().hex[:12]}", system_context=system))
            return reply.text
        except Exception as exc:
            status = f"error:{type(exc).__name__}"
            raise
        finally:
            if self.log_path:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "status": status,
                                        "prompt_sha256": hashlib.sha256((system + user).encode()).hexdigest()[:16],
                                        "prompt_chars": len(system) + len(user),
                                        "seconds": round(time.perf_counter() - t0, 2)}) + "\n")


def build_production_director(registry: ExperimentRegistry, agent: QuantResearchAgent, *, provider=None,
                              log_path: Path | None = None, max_calls: int = 12) -> ResearchDirector:
    def killed() -> bool:
        try:
            from app.config import get_settings
            return bool(getattr(get_settings(), "force_offline", False))
        except Exception:
            return False
    return ResearchDirector(ClaudeResearchLLM(provider, log_path=log_path, max_calls=max_calls, cloud_disabled=killed), registry, agent)
