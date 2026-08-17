"""Local/offline open-weight model runtime, via Ollama's HTTP API.

Independent of ClaudeCodeProvider — Claude does not run inside Ollama, per
ARCHITECTURE.md § Assistant architecture. Requires an already-running local
Ollama daemon (`ollama serve`) with a model pulled; if either is missing,
raises AssistantProviderError rather than hanging or crashing the request.
"""
from __future__ import annotations

import httpx

from assistant.errors import AssistantProviderError
from assistant.provider import AssistantProvider, AssistantReply, AssistantRequest


class OllamaProvider(AssistantProvider):
    name = "ollama"

    def __init__(self, base_url: str, model: str, timeout_seconds: float = 60.0):
        if not model:
            raise ValueError("OllamaProvider requires a model name (OLLAMA_MODEL)")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        messages = []
        if request.system_context:
            messages.append({"role": "system", "content": request.system_context})
        messages.extend(request.history)
        messages.append({"role": "user", "content": request.text})

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/api/chat",
                    json={"model": self._model, "messages": messages, "stream": False},
                )
        except httpx.ConnectError as exc:
            raise AssistantProviderError(
                f"Could not reach Ollama at {self._base_url} — is `ollama serve` running?"
            ) from exc
        except httpx.TimeoutException as exc:
            raise AssistantProviderError("Ollama request timed out") from exc

        if resp.status_code == 404:
            raise AssistantProviderError(
                f"Ollama model '{self._model}' not found — run `ollama pull {self._model}`"
            )
        if resp.status_code != 200:
            raise AssistantProviderError(
                f"Ollama returned HTTP {resp.status_code}: {resp.text[:300]}"
            )

        data = resp.json()
        content = data.get("message", {}).get("content")
        if not content:
            raise AssistantProviderError("Ollama response had no message content")
        return AssistantReply(text=content, provider=self.name)
