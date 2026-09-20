"""Capability-, task-, health-, and latency-aware assistant provider routing."""
from __future__ import annotations

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

TASK_PREFERENCES: dict[ProviderTaskType, tuple[str, ...]] = {
    ProviderTaskType.SIMPLE: ("ollama", "claude_code", "codex", "gemini", "anthropic_api", "mock"),
    ProviderTaskType.REASONING: ("claude_code", "codex", "anthropic_api", "gemini", "ollama"),
    ProviderTaskType.CODING: ("codex", "claude_code", "gemini", "anthropic_api"),
    ProviderTaskType.DEBUGGING: ("codex", "claude_code", "gemini", "anthropic_api"),
    ProviderTaskType.TRADING_EPISTEMICS: ("claude_code", "codex", "anthropic_api", "gemini"),
    ProviderTaskType.FOLLOW_UP: ("claude_code", "codex", "ollama", "gemini", "anthropic_api", "mock"),
    ProviderTaskType.GENERAL: ("ollama", "claude_code", "codex", "gemini", "anthropic_api", "mock"),
}


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
                return (unhealthy, failure_rate, latency, preference)
            return (unhealthy, preference, failure_rate, latency)

        capable.sort(key=rank)
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
            try:
                reply = await provider.respond(request)
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
            emitted = False
            try:
                stream = getattr(provider, "respond_stream", None)
                if stream is None:
                    reply = await provider.respond(request)
                    await self._record_attempt(
                        request=request,
                        provider_id=provider.name,
                        started=started,
                    )
                    yield {"type": "delta", "text": reply.text}
                    yield {"type": "complete", "text": reply.text, "provider": reply.provider}
                    return
                async for event in stream(request):
                    emitted = emitted or bool(event.get("text"))
                    yield event
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
                if emitted:
                    raise
                if index == len(candidates) - 1:
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
