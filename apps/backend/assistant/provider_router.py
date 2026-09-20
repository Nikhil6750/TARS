"""Capability-, task-, health-, and latency-aware assistant provider routing."""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4

from app.latency_store import LatencyTraceStore, RequestTrace
from assistant.errors import AssistantProviderError
from assistant.provider import AssistantProvider, AssistantReply, AssistantRequest
from assistant.provider_health import ProviderHealth, ProviderHealthTracker


class ProviderTaskType(str, Enum):
    SIMPLE = "simple"
    REASONING = "reasoning"
    CODING = "coding"
    DEBUGGING = "debugging"
    TRADING_EPISTEMICS = "trading_epistemics"
    FOLLOW_UP = "follow_up"
    GENERAL = "general"


_CODING = re.compile(
    r"\b(code|python|typescript|javascript|sql|function|class|api|regex|query|"
    r"implement|refactor|compile|typecheck)\b",
    re.IGNORECASE,
)
_DEBUGGING = re.compile(
    r"\b(debug|bug|broken|failure|exception|traceback|stack trace|root cause|"
    r"why (?:is|does|did).*(?:fail|error|hang|crash)|not working)\b",
    re.IGNORECASE,
)
_TRADING = re.compile(
    r"\b(trade|entry|setup|signal|quant_brain|sharpe|drawdown|profitability|"
    r"xauusd|eurusd|market feed|validated trigger)\b",
    re.IGNORECASE,
)
_REASONING = re.compile(
    r"\b(reason|explain|compare|design|evaluate|analy[sz]e|tradeoffs?|causes?|"
    r"safest|plan|architecture)\b",
    re.IGNORECASE,
)
_FOLLOW_UP = re.compile(
    r"^(?:and|also|what about|why|how about|do that|continue|go on|the second|"
    r"make it|shorter|expand|now)\b",
    re.IGNORECASE,
)
_SIMPLE = re.compile(
    r"\b(short|brief|one sentence|two sentences|what is|define|yes or no)\b",
    re.IGNORECASE,
)


CAPABILITIES: dict[str, frozenset[ProviderTaskType]] = {
    "claude_code": frozenset(ProviderTaskType),
    "codex": frozenset(ProviderTaskType),
    "gemini": frozenset(ProviderTaskType),
    "gemini_fast": frozenset(ProviderTaskType),
    "anthropic_api": frozenset(ProviderTaskType),
    "ollama": frozenset(
        {
            ProviderTaskType.SIMPLE,
            ProviderTaskType.REASONING,
            ProviderTaskType.FOLLOW_UP,
            ProviderTaskType.GENERAL,
        }
    ),
    "mock": frozenset({ProviderTaskType.SIMPLE, ProviderTaskType.FOLLOW_UP, ProviderTaskType.GENERAL}),
}

# gemini_fast (direct Gemini Flash text API, no CLI subprocess) is ranked
# first for ordinary conversation -- SIMPLE/FOLLOW_UP/GENERAL/REASONING --
# because Claude Code/Codex CLI startup + turn latency (~4s/~16s, measured)
# is too slow for a voice assistant that should feel immediate.
# REASONING is included deliberately, not an oversight: it's the bucket an
# ordinary "explain X"/"compare X"/"what's the tradeoff" question lands in
# (see _REASONING above) -- physically confirmed "Explain Docker
# containers" and "Explain that more simply" (both explicit examples of
# ordinary conversation this pass targets) classify as REASONING, not
# SIMPLE. It is deliberately NOT preferred for CODING/DEBUGGING/
# TRADING_EPISTEMICS: those are genuinely specialized tasks (writing code,
# debugging, trading epistemics) and stay on Claude/Codex as COMPLEX_TASK
# providers, unchanged. classify_provider_task() checks DEBUGGING/CODING/
# TRADING_EPISTEMICS before REASONING, so a request matching one of those
# still gets classified correctly even if it also contains a REASONING
# keyword like "explain" or "analyze".
TASK_PREFERENCES: dict[ProviderTaskType, tuple[str, ...]] = {
    ProviderTaskType.SIMPLE: ("gemini_fast", "ollama", "claude_code", "codex", "gemini", "anthropic_api", "mock"),
    ProviderTaskType.REASONING: ("gemini_fast", "claude_code", "codex", "anthropic_api", "gemini", "ollama"),
    ProviderTaskType.CODING: ("codex", "claude_code", "gemini", "anthropic_api"),
    ProviderTaskType.DEBUGGING: ("codex", "claude_code", "gemini", "anthropic_api"),
    ProviderTaskType.TRADING_EPISTEMICS: ("claude_code", "codex", "anthropic_api", "gemini"),
    ProviderTaskType.FOLLOW_UP: ("gemini_fast", "claude_code", "codex", "ollama", "gemini", "anthropic_api", "mock"),
    ProviderTaskType.GENERAL: ("gemini_fast", "ollama", "claude_code", "codex", "gemini", "anthropic_api", "mock"),
}

# Width of a "reliability tier" bucket for SIMPLE/GENERAL ranking (see
# rank() below) -- providers whose failure_rate falls in the same 20-point
# band are ranked by latency, not by the raw float difference between them,
# so an occasional transient failure doesn't permanently outrank a
# meaningfully faster provider that is still comparably reliable.
_RELIABILITY_TIER_WIDTH = 0.20

# Hard wall-clock ceiling per provider ATTEMPT, enforced by the router
# regardless of what timeout (if any) the provider's own adapter uses
# internally -- necessary because a provider's own internal timeout can be
# per-chunk rather than total (physically observed: a claude_code call
# stayed alive for 195 seconds because its internal 60s timeout only
# bounded the gap between individual streamed lines, never the whole
# call). gemini_fast/claude_code get the short budgets a latency-sensitive
# voice turn actually needs; any provider not listed here (codex -- "only
# final fallback" -- ollama, gemini, anthropic_api, mock) gets the generous
# default below, which is still a hard, finite bound, never indefinite.
PROVIDER_TIMEOUT_SECONDS: dict[str, float] = {
    "gemini_fast": 5.0,
    "claude_code": 8.0,
}
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 30.0


def _timeout_for(provider_id: str) -> float:
    return PROVIDER_TIMEOUT_SECONDS.get(provider_id, DEFAULT_PROVIDER_TIMEOUT_SECONDS)


# Circuit breaker tuning: distinct from ProviderHealthTracker's long-window
# success rate (used for ranking above) -- this tracks REPEATED RECENT
# failures specifically, so a provider that is failing right now (e.g. a
# live rate limit) is skipped quickly without permanently punishing one
# old, isolated failure the way counting all history ever would.
CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_FAILURE_WINDOW_SECONDS = 120.0
CIRCUIT_COOLDOWN_SECONDS = 60.0


class _CircuitBreaker:
    """In-memory, per-process circuit breaker. Opens (skips) a provider
    after CIRCUIT_FAILURE_THRESHOLD failures within CIRCUIT_FAILURE_WINDOW_SECONDS,
    for CIRCUIT_COOLDOWN_SECONDS, then lets it be tried again -- "temporarily
    skip it, then retry after cooldown," not a permanent demotion."""

    def __init__(self) -> None:
        self._recent_failures: dict[str, list[float]] = {}
        self._open_until: dict[str, float] = {}

    def record_success(self, provider_id: str) -> None:
        self._recent_failures.pop(provider_id, None)
        self._open_until.pop(provider_id, None)

    def record_failure(self, provider_id: str) -> None:
        now = time.monotonic()
        failures = [
            t
            for t in self._recent_failures.get(provider_id, ())
            if now - t < CIRCUIT_FAILURE_WINDOW_SECONDS
        ]
        failures.append(now)
        self._recent_failures[provider_id] = failures
        if len(failures) >= CIRCUIT_FAILURE_THRESHOLD:
            self._open_until[provider_id] = now + CIRCUIT_COOLDOWN_SECONDS

    def is_open(self, provider_id: str) -> bool:
        until = self._open_until.get(provider_id)
        return until is not None and time.monotonic() < until


async def _bounded_stream(agen, timeout: float, provider_name: str):
    """Wraps an async generator with a hard TOTAL wall-clock deadline
    (not per-chunk) -- see PROVIDER_TIMEOUT_SECONDS above for why a
    provider's own internal per-chunk timeout isn't sufficient on its own.
    Cancels the underlying generator's task the moment the deadline is
    exceeded so the caller can fail over to the next candidate immediately
    rather than waiting for the provider to give up on its own."""
    queue: asyncio.Queue = asyncio.Queue()

    async def _drain() -> None:
        try:
            async for event in agen:
                queue.put_nowait(("event", event))
            queue.put_nowait(("done", None))
        except Exception as exc:  # noqa: BLE001 -- surfaced to the consumer below
            queue.put_nowait(("error", exc))

    drain_task = asyncio.create_task(_drain())
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssistantProviderError(f"{provider_name} exceeded {timeout}s timeout")
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=remaining)
            except TimeoutError as exc:
                raise AssistantProviderError(f"{provider_name} exceeded {timeout}s timeout") from exc
            if kind == "event":
                yield payload
            elif kind == "error":
                raise payload
            else:
                return
    finally:
        if not drain_task.done():
            drain_task.cancel()


@dataclass(frozen=True)
class ProviderRouteDecision:
    task_type: ProviderTaskType
    ordered_provider_ids: tuple[str, ...]
    reason: str


def classify_provider_task(request: AssistantRequest) -> ProviderTaskType:
    text = request.text.strip()
    if request.history and _FOLLOW_UP.search(text):
        return ProviderTaskType.FOLLOW_UP
    if _DEBUGGING.search(text):
        return ProviderTaskType.DEBUGGING
    if _CODING.search(text):
        return ProviderTaskType.CODING
    if _TRADING.search(text):
        return ProviderTaskType.TRADING_EPISTEMICS
    if _REASONING.search(text):
        return ProviderTaskType.REASONING
    if _SIMPLE.search(text) or len(text.split()) <= 10:
        return ProviderTaskType.SIMPLE
    return ProviderTaskType.GENERAL


class RoutedAssistantProvider(AssistantProvider):
    """Select one capable provider and fail over only on real provider failure."""

    name = "adaptive"

    def __init__(
        self,
        providers: list[AssistantProvider],
        *,
        trace_store: LatencyTraceStore | None = None,
        fixed_order: bool = False,
    ) -> None:
        if not providers:
            raise ValueError("at least one assistant provider is required")
        self._providers = {provider.name: provider for provider in providers}
        self._trace_store = trace_store
        self._health = ProviderHealthTracker(trace_store) if trace_store is not None else None
        self._fixed_order = fixed_order
        self._circuit = _CircuitBreaker()
        self.last_decision: ProviderRouteDecision | None = None

    async def _ordered_candidates(self, request: AssistantRequest) -> list[AssistantProvider]:
        task_type = classify_provider_task(request)
        if self._fixed_order:
            candidates = [
                provider
                for provider in self._providers.values()
                if bool(getattr(provider, "is_available", True))
            ]
            if not candidates:
                candidates = list(self._providers.values())
            self.last_decision = ProviderRouteDecision(
                task_type=task_type,
                ordered_provider_ids=tuple(provider.name for provider in candidates),
                reason="explicit primary order; fallback only after provider failure",
            )
            return candidates

        capable = [
            provider
            for provider in self._providers.values()
            if task_type in CAPABILITIES.get(provider.name, frozenset(ProviderTaskType))
            and bool(getattr(provider, "is_available", True))
        ]
        if not capable:
            capable = list(self._providers.values())

        preferences = TASK_PREFERENCES[task_type]
        preference_rank = {name: index for index, name in enumerate(preferences)}
        health: dict[str, ProviderHealth] = {}
        if self._health is not None:
            for provider in capable:
                health[provider.name] = await self._health.health_for(
                    provider.name, kind="provider_route"
                )

        def rank(provider: AssistantProvider) -> tuple[float, ...]:
            stats = health.get(provider.name)
            preference = float(preference_rank.get(provider.name, len(preferences)))
            if stats is None or stats.sample_size == 0:
                # Unknown providers remain eligible in task-preference order.
                return (0.0, preference, 1.0, float("inf"))
            unhealthy = 1.0 if stats.sample_size >= 3 and stats.success_rate < 0.6 else 0.0
            failure_rate = 1.0 - stats.success_rate
            latency = stats.p50_ms if stats.p50_ms is not None else float("inf")
            if task_type in (ProviderTaskType.SIMPLE, ProviderTaskType.GENERAL):
                # Bucketed, not raw, failure_rate: sorting on the exact float
                # meant one stale recorded failure (e.g. a single transient
                # API blip minutes or hours ago) permanently outranked a
                # provider with a real multi-second latency advantage --
                # physically observed with gemini_fast (93% success, ~3.4s
                # p50) losing every ordinary-conversation turn to codex
                # (100% success, ~10s p50) after exactly one recorded
                # failure, defeating the entire point of ranking gemini_fast
                # first for latency-sensitive SIMPLE/GENERAL turns. Providers
                # within the same reliability tier (bucket width below) are
                # treated as equally reliable and broken by latency instead;
                # a genuinely worse track record (a full tier down) still
                # loses regardless of latency.
                reliability_tier = float(int(failure_rate / _RELIABILITY_TIER_WIDTH))
                return (unhealthy, reliability_tier, latency, preference)
            return (unhealthy, preference, failure_rate, latency)

        capable.sort(key=rank)
        # Circuit-open providers (repeated RECENT failures -- see
        # _CircuitBreaker) are pushed to the end, not removed outright: a
        # provider currently failing a lot should be tried last, but if
        # every candidate is circuit-open it's still better to attempt the
        # least-recently-failing one than to fail the whole turn with zero
        # attempts. Stable sort preserves the health-based order above
        # within each open/closed group.
        capable.sort(key=lambda provider: self._circuit.is_open(provider.name))
        self.last_decision = ProviderRouteDecision(
            task_type=task_type,
            ordered_provider_ids=tuple(provider.name for provider in capable),
            reason="task capability filtered; unhealthy providers demoted; recorded latency breaks healthy ties",
        )
        return capable

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        candidates = await self._ordered_candidates(request)
        failures: list[AssistantProviderError] = []
        for index, provider in enumerate(candidates):
            started = time.monotonic()
            timeout = _timeout_for(provider.name)
            try:
                try:
                    reply = await asyncio.wait_for(provider.respond(request), timeout=timeout)
                except TimeoutError as exc:
                    raise AssistantProviderError(
                        f"{provider.name} exceeded {timeout}s router timeout"
                    ) from exc
                if not reply.text.strip():
                    # A CLI provider can exit 0 with no usable result text
                    # (observed physically, not hypothetical) without ever
                    # raising -- treat that exactly like a real failure so
                    # it falls over to the next candidate instead of being
                    # accepted as a successful empty answer.
                    raise AssistantProviderError(
                        f"{provider.name} returned an empty response"
                    )
            except AssistantProviderError as exc:
                failures.append(exc)
                await self._record_attempt(
                    request=request,
                    provider_id=provider.name,
                    started=started,
                    error=type(exc).__name__,
                )
                continue
            await self._record_attempt(
                request=request,
                provider_id=provider.name,
                started=started,
            )
            if reply.diagnostics is not None:
                reply.diagnostics.fallback_used = index > 0
            return reply
        raise AssistantProviderError(
            f"All {len(candidates)} capable assistant providers failed"
        ) from (failures[-1] if failures else None)

    async def respond_stream(self, request: AssistantRequest):
        candidates = await self._ordered_candidates(request)
        for index, provider in enumerate(candidates):
            started = time.monotonic()
            timeout = _timeout_for(provider.name)
            emitted_text = ""
            is_last = index == len(candidates) - 1
            try:
                stream = getattr(provider, "respond_stream", None)
                if stream is None:
                    try:
                        reply = await asyncio.wait_for(provider.respond(request), timeout=timeout)
                    except TimeoutError as exc:
                        raise AssistantProviderError(
                            f"{provider.name} exceeded {timeout}s router timeout"
                        ) from exc
                    if not reply.text.strip():
                        raise AssistantProviderError(
                            f"{provider.name} returned an empty response"
                        )
                    await self._record_attempt(
                        request=request,
                        provider_id=provider.name,
                        started=started,
                    )
                    yield {"type": "delta", "text": reply.text}
                    yield {"type": "complete", "text": reply.text, "provider": reply.provider}
                    return
                async for event in _bounded_stream(stream(request), timeout, provider.name):
                    if event.get("type") == "delta":
                        emitted_text += str(event.get("text") or "")
                    elif event.get("type") == "complete":
                        emitted_text = str(event.get("text") or emitted_text)
                    yield event
                if not emitted_text.strip():
                    # Same silent-empty-success gap as above, but for the
                    # streaming adapter path (what normal-conversation
                    # voice/text turns actually use) -- a provider whose
                    # stream ends with no usable text was never flagged as
                    # a failure before this check, so it never fell over to
                    # a healthy alternate provider. Physically observed:
                    # "Explain Docker containers" silently produced no
                    # text, and the caller had no signal to retry elsewhere.
                    raise AssistantProviderError(
                        f"{provider.name} completed with no usable response text"
                    )
                await self._record_attempt(
                    request=request,
                    provider_id=provider.name,
                    started=started,
                )
                return
            except AssistantProviderError as exc:
                await self._record_attempt(
                    request=request,
                    provider_id=provider.name,
                    started=started,
                    error=type(exc).__name__,
                )
                if emitted_text.strip():
                    # Real partial content already reached the caller --
                    # switching providers now would append a second,
                    # unrelated answer after it, so surface the error
                    # instead of silently retrying elsewhere.
                    raise
                if is_last:
                    raise AssistantProviderError(
                        f"All {len(candidates)} capable assistant providers failed"
                    ) from exc

    async def _record_attempt(
        self,
        *,
        request: AssistantRequest,
        provider_id: str,
        started: float,
        error: str | None = None,
    ) -> None:
        if error is not None:
            self._circuit.record_failure(provider_id)
        else:
            self._circuit.record_success(provider_id)
        if self._trace_store is None:
            return
        await self._trace_store.record(
            RequestTrace(
                request_id=uuid4().hex,
                kind="provider_route",
                conversation_id=request.conversation_id,
                provider_id=provider_id,
                started_at=datetime.now(UTC).isoformat(),
                total_ms=round((time.monotonic() - started) * 1000, 2),
                error=error,
            )
        )
