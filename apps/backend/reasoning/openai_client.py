"""OpenAI Responses API client with two supported authentication sources:

  * ApiKeyAuth  - an official OpenAI API key (Authorization: Bearer <key>)
  * SiwcAuth    - 'Sign in with ChatGPT' plan usage (OAuth access token from reasoning.siwc)

Both talk to POST https://api.openai.com/v1/responses. In SIWC mode the docs require stream=true and store=false on every request; this
client uses the same body in both modes. A call only succeeds after `response.completed`. No secrets are logged, and error text is
scrubbed of anything that looks like a credential."""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol

import httpx

BASE_URL = "https://api.openai.com/v1"
USAGE_LIMIT_CODES = {"subscription_sharing_usage_limit_exceeded", "subscription_sharing_usage_unavailable", "insufficient_quota", "rate_limit_exceeded"}
_SECRET = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{12,}|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*)")


def scrub(text: str) -> str:
    return _SECRET.sub("<redacted>", text or "")[:600]


class OpenAIError(RuntimeError):
    kind = "error"


class OpenAIAuthError(OpenAIError):
    kind = "auth"          # missing / invalid / revoked / expired-and-unrefreshable credentials


class OpenAIUsageLimit(OpenAIError):
    kind = "usage_limit"   # plan or quota exhausted (may arrive mid-stream)


class OpenAIUnavailable(OpenAIError):
    kind = "unavailable"   # timeout, network failure, 5xx, interrupted stream, incomplete response


class OpenAICancelled(OpenAIError):
    kind = "cancelled"


class OpenAINotEligible(OpenAIError):
    kind = "not_eligible"   # subscription_sharing_user_not_eligible (403): explain the restriction; do NOT repeat the request or loop through OAuth


class OpenAIUnsupported(OpenAIError):
    kind = "unsupported"    # subscription_sharing_unsupported_capability (400) / route not supported: fix the request, never retry the same body


# Documented structured codes (SIWC 'Errors and recovery'). Everything not listed falls through to status-based handling.
_CODE_MAP = {
    "subscription_sharing_user_not_eligible": OpenAINotEligible,
    "subscription_sharing_usage_limit_exceeded": OpenAIUsageLimit,
    "subscription_sharing_usage_unavailable": OpenAIUnavailable,
    "subscription_sharing_user_unavailable": OpenAIUnavailable,
    "subscription_sharing_unsupported_capability": OpenAIUnsupported,
    "subscription_sharing_route_not_supported": OpenAIUnsupported,
    "subscription_sharing_invalid_user": OpenAIAuthError,
    "chatpass_v2_scope_not_authorized": OpenAIAuthError,
    "chatpass_v2_invalid_authorization_context": OpenAIAuthError,
}


def raise_for_code(code: str, detail: str = "") -> None:
    exc = _CODE_MAP.get(code)
    if exc is not None:
        raise exc(f"{code}{': ' + scrub(detail) if detail else ''}")
    if code in USAGE_LIMIT_CODES:
        raise OpenAIUsageLimit(code)


class AuthSource(Protocol):
    mode: str

    async def bearer(self) -> str: ...
    async def on_unauthorized(self) -> bool: ...   # True if credentials were refreshed and the call may be retried once


@dataclass
class ApiKeyAuth:
    key: str
    mode: str = "api_key"

    async def bearer(self) -> str:
        if not self.key:
            raise OpenAIAuthError("no OpenAI API key configured")
        return self.key

    async def on_unauthorized(self) -> bool:
        return False


@dataclass
class ModelInfo:
    id: str
    display_name: str = ""


@dataclass
class Completion:
    text: str
    model: str
    seconds: float
    first_token_seconds: float | None
    usage: dict = field(default_factory=dict)


class _SharedCtx:
    """Async-context wrapper that hands out a long-lived client WITHOUT closing it on exit."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._c = client

    async def __aenter__(self) -> httpx.AsyncClient:
        return self._c

    async def __aexit__(self, *exc) -> bool:
        return False


class OpenAIResponsesClient:
    def __init__(self, auth: AuthSource, *, base_url: str = BASE_URL, timeout: float = 60.0, transport: httpx.AsyncBaseTransport | None = None,
                 persistent: bool = True) -> None:
        self.auth, self.base_url, self.timeout, self._transport = auth, base_url.rstrip("/"), timeout, transport
        self.persistent = persistent and transport is None     # keep-alive saves a TLS handshake on every call; tests with mock transports stay per-call
        self._shared: httpx.AsyncClient | None = None
        self._shared_loop: asyncio.AbstractEventLoop | None = None

    def _client(self):
        timeout = httpx.Timeout(self.timeout, connect=10.0)
        if not self.persistent:
            return httpx.AsyncClient(timeout=timeout, transport=self._transport)
        loop = asyncio.get_running_loop()
        if self._shared is None or self._shared_loop is not loop or self._shared.is_closed:
            self._shared, self._shared_loop = httpx.AsyncClient(timeout=timeout, limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=120)), loop
        return _SharedCtx(self._shared)

    async def aclose(self) -> None:
        if self._shared is not None and not self._shared.is_closed and self._shared_loop is asyncio.get_running_loop():
            await self._shared.aclose()
        self._shared = None

    async def _headers(self) -> dict:
        return {"Authorization": f"Bearer {await self.auth.bearer()}", "Content-Type": "application/json"}

    # ---- model discovery (from the ACCOUNT's real entitlements) ----------------------------------------------------
    async def list_models(self) -> list[ModelInfo]:
        for attempt in (0, 1):
            try:
                async with self._client() as c:
                    r = await c.get(f"{self.base_url}/models", headers=await self._headers())
            except httpx.TimeoutException as exc:
                raise OpenAIUnavailable("model listing timed out") from exc
            except httpx.HTTPError as exc:
                raise OpenAIUnavailable(f"model listing failed: {type(exc).__name__}") from exc
            if r.status_code == 401:
                if attempt == 0 and await self.auth.on_unauthorized():
                    continue
                raise OpenAIAuthError("OpenAI rejected the credentials (401): sign in again or fix the API key")
            if r.status_code == 429:
                raise OpenAIUsageLimit("usage limit while listing models")
            if r.status_code >= 400:
                raise OpenAIUnavailable(f"model listing HTTP {r.status_code}: {scrub(r.text)}")
            body = r.json()
            if self.auth.mode == "siwc":   # documented shape: {"models": [{"slug","display_name","visibility"}]}
                return [ModelInfo(m["slug"], m.get("display_name", "")) for m in body.get("models", []) if m.get("visibility") == "list"]
            return [ModelInfo(m["id"], m["id"]) for m in body.get("data", [])]
        raise OpenAIAuthError("unreachable")

    # ---- streaming inference ---------------------------------------------------------------------------------------
    async def stream(self, model: str, system: str, user: str, *, max_output_tokens: int | None = 1500,
                     cancel: asyncio.Event | None = None, reasoning_effort: str | None = None, history: list[dict] | None = None) -> AsyncIterator[tuple[str, str]]:
        """Yield ('delta', text) chunks, then a final ('completed', full_text). Raises typed OpenAIError on any other terminal state."""
        turns = [{"role": t["role"], "content": t["content"]} for t in (history or []) if t.get("role") in ("user", "assistant")]
        body = {"model": model, "input": [{"role": "developer", "content": system}, *turns, {"role": "user", "content": user}], "stream": True, "store": False}
        if reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort}
        if max_output_tokens and self.auth.mode != "siwc":     # the plan-usage route REJECTS max_output_tokens (preview limitations)
            body["max_output_tokens"] = max_output_tokens
        for attempt in (0, 1):
            parts: list[str] = []
            try:
                async with self._client() as c:
                    async with c.stream("POST", f"{self.base_url}/responses", headers=await self._headers(), json=body) as r:
                        if r.status_code == 401:
                            await r.aread()
                            if attempt == 0 and await self.auth.on_unauthorized():
                                continue
                            raise OpenAIAuthError("OpenAI rejected the credentials (401): sign in again or fix the API key")
                        if r.status_code == 429:
                            await r.aread()
                            raise OpenAIUsageLimit("OpenAI rate/usage limit (429)")
                        if r.status_code >= 400:
                            raw = (await r.aread()).decode("utf-8", "replace")
                            code = _error_code(raw)
                            raise_for_code(code, _error_detail(raw))
                            if r.status_code == 403:
                                raise OpenAINotEligible(f"HTTP 403 admission refused: {scrub(raw)}")     # policy/region/permission check
                            raise OpenAIUnavailable(f"HTTP {r.status_code}: {scrub(raw)}") if r.status_code >= 500 else OpenAIError(f"HTTP {r.status_code}: {scrub(raw)}")
                        event = None
                        async for line in r.aiter_lines():
                            if cancel is not None and cancel.is_set():
                                raise OpenAICancelled("cancelled by caller")
                            if line.startswith("event:"):
                                event = line[6:].strip()
                                continue
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                continue
                            try:
                                obj = json.loads(data)
                            except ValueError:
                                continue
                            kind = obj.get("type") or event
                            if kind == "response.output_text.delta":
                                parts.append(obj.get("delta", ""))
                                yield "delta", obj.get("delta", "")
                            elif kind == "response.completed":
                                final = "".join(parts)
                                resp = obj.get("response", {})
                                yield "completed", final
                                yield "usage", json.dumps(resp.get("usage", {}))
                                return
                            elif kind == "response.failed":
                                code = (obj.get("response", {}).get("error") or obj.get("error") or {}).get("code", "")
                                raise_for_code(code)
                                raise OpenAIUnavailable(f"response.failed: {scrub(str(code))}")
                            elif kind == "response.incomplete":
                                raise OpenAIUnavailable("response.incomplete: " + scrub(str(obj.get("response", {}).get("incomplete_details", ""))))
                            elif kind == "error":
                                code = (obj.get("error") or obj).get("code", "") if isinstance(obj.get("error") or obj, dict) else ""
                                raise_for_code(code)
                                raise OpenAIUnavailable("stream error: " + scrub(str(code)))
                        raise OpenAIUnavailable("stream ended before response.completed")
            except httpx.TimeoutException as exc:
                raise OpenAIUnavailable("request timed out") from exc
            except httpx.HTTPError as exc:
                raise OpenAIUnavailable(f"network failure: {type(exc).__name__}") from exc
        raise OpenAIAuthError("unreachable")

    async def complete(self, model: str, system: str, user: str, *, max_output_tokens: int | None = 1500,
                       cancel: asyncio.Event | None = None, deadline: float | None = None, reasoning_effort: str | None = None) -> Completion:
        t0 = time.perf_counter()
        first: float | None = None
        text, usage = "", {}

        async def run():
            nonlocal first, text, usage
            async for kind, payload in self.stream(model, system, user, max_output_tokens=max_output_tokens, cancel=cancel, reasoning_effort=reasoning_effort):
                if kind == "delta" and first is None:
                    first = time.perf_counter() - t0
                elif kind == "completed":
                    text = payload
                elif kind == "usage":
                    usage = json.loads(payload)
        try:
            await asyncio.wait_for(run(), timeout=deadline) if deadline else await run()
        except asyncio.TimeoutError as exc:
            raise OpenAIUnavailable(f"deadline of {deadline}s exceeded") from exc
        return Completion(text, model, time.perf_counter() - t0, first, usage)


def _error_detail(raw: str) -> str:
    try:
        err = json.loads(raw).get("error", {})
        return err.get("param") or err.get("message") or ""
    except ValueError:
        return ""


def _error_code(raw: str) -> str:
    try:
        err = json.loads(raw).get("error", {})
        return err.get("code") or err.get("type") or ""
    except ValueError:
        return ""
