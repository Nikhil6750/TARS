"""Direct, low-latency Gemini Flash text generation -- TARS's default
provider for ordinary conversation. NOT Gemini Live audio (see
voice/gemini_live_loop.py, which is ears-only and never touches this
module): no realtime session, no audio in or out, just one text-in/
text-out API call per turn, the same shape as the Claude Code / Codex CLI
adapters it exists to replace on the ordinary-conversation hot path.

Physically measured on this machine (see the golden-loop latency-pass
report): Claude Code CLI subprocess startup + turn ~4s, Codex CLI ~16s --
fundamentally too slow for a voice assistant that should feel like Siri/
Alexa for a plain question. This path measured 1.5-2.6s for the same
prompts. Claude/Codex remain available as COMPLEX_TASK providers (coding,
debugging, deep reasoning, trading epistemics) via the existing
provider_router.py task classification -- this provider is simply ranked
first for SIMPLE/GENERAL/FOLLOW_UP task types, not a replacement for them.

Uses the SYNCHRONOUS google-genai client (via asyncio.to_thread / a
background thread for streaming), not the async client: the async client's
aiohttp-based transport hits a Windows-specific DNS resolution bug
(`aiohttp.ClientConnectorDNSError: ... Could not contact DNS servers`)
reproduced consistently on this machine, even though system DNS resolution
(nslookup, socket.gethostbyname, urllib) all work fine at the same time --
this is specifically an aiohttp/Windows interaction, not a real network
problem. The sync client's httpx-based transport does not hit it.
"""
from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import AsyncIterator

from assistant.errors import AssistantProviderError
from assistant.provider import (
    AssistantProvider,
    AssistantReply,
    AssistantRequest,
    ProviderDiagnostics,
    render_provider_prompt,
)

DEFAULT_MODEL = "gemini-3.5-flash"


class GeminiFastConversationProvider(AssistantProvider):
    name = "gemini_fast"

    def __init__(self, *, model: str = DEFAULT_MODEL, api_key: str | None = None) -> None:
        self._model = model
        resolved_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self._client = None
        if resolved_key:
            from google import genai

            self._client = genai.Client(api_key=resolved_key)

    @property
    def is_available(self) -> bool:
        return self._client is not None

    def _build_config(self, request: AssistantRequest):
        if not request.system_context:
            return None
        from google.genai import types

        return types.GenerateContentConfig(system_instruction=request.system_context)

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        client = self._client
        if client is None:
            raise AssistantProviderError(
                "gemini_fast: no GEMINI_API_KEY/GOOGLE_API_KEY in environment"
            )
        prompt = render_provider_prompt(request)
        config = self._build_config(request)

        def _call() -> str:
            response = client.models.generate_content(
                model=self._model, contents=prompt, config=config
            )
            return response.text or ""

        try:
            text = await asyncio.to_thread(_call)
        except Exception as exc:
            raise AssistantProviderError(f"gemini_fast request failed: {exc}") from exc
        if not text.strip():
            raise AssistantProviderError("gemini_fast returned an empty response")
        return AssistantReply(
            text=text.strip(),
            provider=self.name,
            diagnostics=ProviderDiagnostics(provider_id=self.name, model=self._model),
        )

    async def respond_stream(self, request: AssistantRequest) -> AsyncIterator[dict]:
        client = self._client
        if client is None:
            raise AssistantProviderError(
                "gemini_fast: no GEMINI_API_KEY/GOOGLE_API_KEY in environment"
            )
        prompt = render_provider_prompt(request)
        config = self._build_config(request)

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()

        def _stream_worker() -> None:
            try:
                for chunk in client.models.generate_content_stream(
                    model=self._model, contents=prompt, config=config
                ):
                    if chunk.text:
                        loop.call_soon_threadsafe(queue.put_nowait, ("delta", chunk.text))
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))
            except Exception as exc:  # noqa: BLE001 -- surfaced to the async side below
                loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))

        # generate_content_stream's iterator blocks on network I/O per
        # chunk, so it runs on its own thread; chunks are bridged back to
        # this coroutine via the queue as they arrive, which is what makes
        # this a real stream (first sentence speakable before the rest of
        # the reply has even been generated) rather than a fake one that
        # just buffers the whole response before yielding anything.
        threading.Thread(target=_stream_worker, daemon=True).start()

        accumulated = ""
        while True:
            kind, payload = await queue.get()
            if kind == "delta":
                assert isinstance(payload, str)
                accumulated += payload
                yield {"type": "delta", "text": payload}
            elif kind == "error":
                raise AssistantProviderError(f"gemini_fast stream failed: {payload}") from (
                    payload if isinstance(payload, BaseException) else None
                )
            else:
                break

        if not accumulated.strip():
            raise AssistantProviderError("gemini_fast returned an empty response")
        yield {"type": "complete", "text": accumulated.strip(), "provider": self.name}
