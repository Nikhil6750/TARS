from __future__ import annotations

import asyncio
import time

import aiosqlite
import pytest

from app.latency_store import LatencyTraceStore, RequestTrace
from assistant.errors import AssistantProviderError
from assistant.provider import (
    AssistantProvider,
    AssistantReply,
    AssistantRequest,
    ProviderDiagnostics,
)
from assistant.provider_router import (
    ProviderTaskType,
    RoutedAssistantProvider,
    _CircuitBreaker,
    classify_provider_task,
)
import assistant.provider_router as provider_router
from storage.migrator import run_migrations


class _Provider(AssistantProvider):
    def __init__(self, name: str, *, fail: bool = False, empty: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.empty = empty
        self.calls = 0
        self.is_available = True

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        self.calls += 1
        if self.fail:
            raise AssistantProviderError("internal executable failure")
        if self.empty:
            # Reproduces the physically observed Claude/Codex CLI behavior
            # of exiting 0 with no usable result text, without raising.
            return AssistantReply(text="", provider=self.name)
        return AssistantReply(
            text=f"answered by {self.name}",
            provider=self.name,
            diagnostics=ProviderDiagnostics(provider_id=self.name),
        )


class _StreamProvider(AssistantProvider):
    """A provider whose stream completes with no usable text but never
    raises -- the streaming-adapter counterpart of `_Provider(empty=True)`,
    since ClaudeCodeProvider.respond_stream() can end this way too."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0
        self.is_available = True

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        raise AssertionError(f"{self.name}.respond() should not be called when respond_stream exists")

    async def respond_stream(self, request: AssistantRequest):
        self.calls += 1
        yield {"type": "complete", "text": "", "provider": self.name}


@pytest.fixture
async def trace_store(tmp_path):
    path = tmp_path / "router.db"
    run_migrations(path)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    yield LatencyTraceStore(conn)
    await conn.close()


def _request(text: str, *, history=None) -> AssistantRequest:
    return AssistantRequest(
        text=text,
        conversation_id="conversation",
        history=history or [],
    )


def test_task_classifier_covers_capability_routes_and_followups():
    assert classify_provider_task(_request("Write a Python function")) is ProviderTaskType.CODING
    assert classify_provider_task(_request("Debug why this service crashes")) is ProviderTaskType.DEBUGGING
    assert classify_provider_task(_request("Should I enter this trade?")) is ProviderTaskType.TRADING_EPISTEMICS
    assert classify_provider_task(
        _request("What about the second option?", history=[{"role": "assistant", "content": "Two options"}])
    ) is ProviderTaskType.FOLLOW_UP


async def test_router_uses_task_capability_instead_of_sending_everything_to_codex():
    claude = _Provider("claude_code")
    codex = _Provider("codex")
    router = RoutedAssistantProvider([claude, codex])

    reasoning = await router.respond(_request("Explain the tradeoffs in this design"))
    coding = await router.respond(_request("Write a Python function for this"))

    assert reasoning.provider == "claude_code"
    assert coding.provider == "codex"


async def test_router_demotes_unhealthy_provider_and_uses_recorded_latency(trace_store):
    for index in range(3):
        await trace_store.record(
            RequestTrace(
                request_id=f"claude-fail-{index}",
                kind="provider_route",
                provider_id="claude_code",
                started_at="2026-08-21T00:00:00Z",
                total_ms=100,
                error="failure",
            )
        )
        await trace_store.record(
            RequestTrace(
                request_id=f"codex-ok-{index}",
                kind="provider_route",
                provider_id="codex",
                started_at="2026-08-21T00:00:00Z",
                total_ms=800,
            )
        )

    router = RoutedAssistantProvider(
        [_Provider("claude_code"), _Provider("codex")],
        trace_store=trace_store,
    )
    reply = await router.respond(_request("Explain this architecture"))

    assert reply.provider == "codex"
    assert router.last_decision is not None
    assert router.last_decision.ordered_provider_ids[0] == "codex"


async def test_router_prefers_fast_provider_over_one_stale_failure(trace_store):
    """Regression test for a physically observed bug: a single old,
    transient failure recorded against an otherwise fast, reliable
    provider (gemini_fast: 14/15 = 93% success, ~3.5s) was permanently
    outranking it below a provider with a perfect but much slower record
    (codex: 100% success, ~10s) for SIMPLE/GENERAL tasks -- because ranking
    sorted on the raw failure_rate float before latency, so ANY nonzero
    failure_rate difference (however small) beat ANY latency difference
    (however large). Both providers here are comparably reliable (well
    within the same reliability tier), so the faster one must win."""
    for index in range(14):
        await trace_store.record(
            RequestTrace(
                request_id=f"fast-ok-{index}",
                kind="provider_route",
                provider_id="fast",
                started_at="2026-08-21T00:00:00Z",
                total_ms=3500,
            )
        )
    await trace_store.record(
        RequestTrace(
            request_id="fast-fail-0",
            kind="provider_route",
            provider_id="fast",
            started_at="2026-08-21T00:00:00Z",
            total_ms=1500,
            error="transient",
        )
    )
    for index in range(20):
        await trace_store.record(
            RequestTrace(
                request_id=f"slow-ok-{index}",
                kind="provider_route",
                provider_id="slow",
                started_at="2026-08-21T00:00:00Z",
                total_ms=10000,
            )
        )

    router = RoutedAssistantProvider(
        [_Provider("slow"), _Provider("fast")],
        trace_store=trace_store,
    )
    reply = await router.respond(_request("What is a container?"))

    assert reply.provider == "fast"
    assert router.last_decision is not None
    assert router.last_decision.ordered_provider_ids[0] == "fast"


class _HangingProvider(AssistantProvider):
    """Simulates a provider that hangs well past any reasonable timeout --
    e.g. the physically observed claude_code call that stayed alive for
    195 seconds. Its own internal delay (`hang_seconds`) is deliberately
    much longer than the router timeouts under test, so a passing test
    proves the router's timeout actually cut it off rather than the
    provider happening to finish quickly on its own."""

    is_available = True

    def __init__(self, name: str, *, hang_seconds: float = 5.0, streaming: bool = False) -> None:
        self.name = name
        self.hang_seconds = hang_seconds
        self.streaming = streaming
        self.calls = 0

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        self.calls += 1
        await asyncio.sleep(self.hang_seconds)
        return AssistantReply(text="too slow", provider=self.name)

    async def respond_stream(self, request: AssistantRequest):
        if not self.streaming:
            raise AssertionError("respond_stream should not be called for a non-streaming hang test")
        self.calls += 1
        # No delta yielded before the hang, matching what was physically
        # observed: the claude_code CLI produced NO usable output for the
        # entire 195 seconds before eventually failing -- if it had
        # already streamed real partial content, the router's existing
        # (and correct, separately tested) "don't retry after partial
        # content" rule means it must NOT fail over, since switching
        # providers mid-answer would splice two different answers
        # together.
        await asyncio.sleep(self.hang_seconds)
        yield {"type": "complete", "text": "never reached", "provider": self.name}


async def test_respond_times_out_and_falls_over_fast_not_after_the_full_hang(monkeypatch):
    monkeypatch.setattr(provider_router, "DEFAULT_PROVIDER_TIMEOUT_SECONDS", 0.05)
    hanging = _HangingProvider("hangs", hang_seconds=5.0)
    codex = _Provider("codex")
    router = RoutedAssistantProvider([hanging, codex], fixed_order=True)

    started = time.monotonic()
    reply = await router.respond(_request("Write Python code"))
    elapsed = time.monotonic() - started

    assert reply.provider == "codex"
    assert elapsed < 2.0, f"router waited {elapsed:.2f}s -- should be bounded by the ~0.05s timeout, not the 5s hang"


async def test_respond_stream_times_out_and_falls_over_fast_not_after_the_full_hang(monkeypatch):
    """Regression test for the physically observed bug: a claude_code
    stream stayed alive 195 seconds because its own internal timeout only
    bounded the gap between individual streamed lines, never the whole
    call. A provider here yields once (so it is genuinely mid-stream, not
    just slow to start) and then hangs -- the router must still fail over
    within its configured budget, not wait for the hang to end."""
    monkeypatch.setattr(provider_router, "DEFAULT_PROVIDER_TIMEOUT_SECONDS", 0.1)
    hanging = _HangingProvider("hangs", hang_seconds=5.0, streaming=True)
    codex = _Provider("codex")
    router = RoutedAssistantProvider([hanging, codex], fixed_order=True)

    started = time.monotonic()
    events = [event async for event in router.respond_stream(_request("Write Python code"))]
    elapsed = time.monotonic() - started

    complete = [e for e in events if e.get("type") == "complete"]
    assert complete[-1]["provider"] == "codex"
    assert elapsed < 2.0, f"router waited {elapsed:.2f}s -- should be bounded by the ~0.1s timeout, not the 5s hang"


def test_circuit_breaker_opens_after_repeated_recent_failures_and_recovers_after_cooldown(monkeypatch):
    fake_now = [1_000.0]
    monkeypatch.setattr(provider_router.time, "monotonic", lambda: fake_now[0])

    breaker = _CircuitBreaker()
    assert breaker.is_open("flaky") is False

    for _ in range(provider_router.CIRCUIT_FAILURE_THRESHOLD):
        breaker.record_failure("flaky")
    assert breaker.is_open("flaky") is True

    fake_now[0] += provider_router.CIRCUIT_COOLDOWN_SECONDS + 1
    assert breaker.is_open("flaky") is False, "circuit must reopen for retry after the cooldown elapses"


def test_circuit_breaker_never_permanently_punishes_one_old_failure():
    breaker = _CircuitBreaker()
    breaker.record_failure("gemini_fast")
    assert breaker.is_open("gemini_fast") is False
    breaker.record_success("gemini_fast")
    assert breaker.is_open("gemini_fast") is False


async def test_router_demotes_circuit_open_provider_below_a_healthy_one(trace_store):
    router = RoutedAssistantProvider(
        [_Provider("flaky"), _Provider("steady")],
        trace_store=trace_store,
    )
    for _ in range(provider_router.CIRCUIT_FAILURE_THRESHOLD):
        router._circuit.record_failure("flaky")

    candidates = await router._ordered_candidates(_request("What is a container?"))

    assert [p.name for p in candidates][0] == "steady"


async def test_router_fails_over_and_marks_diagnostics():
    codex = _Provider("codex", fail=True)
    claude = _Provider("claude_code")
    router = RoutedAssistantProvider([codex, claude])

    reply = await router.respond(_request("Write Python code"))

    assert reply.provider == "claude_code"
    assert reply.diagnostics is not None
    assert reply.diagnostics.fallback_used is True
    assert codex.calls == 1
    assert claude.calls == 1


async def test_fixed_order_keeps_explicit_primary_then_falls_back():
    claude = _Provider("claude_code", fail=True)
    codex = _Provider("codex")
    router = RoutedAssistantProvider([claude, codex], fixed_order=True)

    reply = await router.respond(_request("Write Python code"))

    assert reply.provider == "codex"
    assert router.last_decision is not None
    assert router.last_decision.ordered_provider_ids == ("claude_code", "codex")
    assert claude.calls == 1
    assert codex.calls == 1


async def test_respond_falls_over_on_empty_reply_without_error():
    """Physically observed: 'Explain Docker containers' silently returned
    no text from claude_code with returncode 0 -- no exception, so the
    router must treat that exactly like a real failure and try codex."""
    claude = _Provider("claude_code", empty=True)
    codex = _Provider("codex")
    router = RoutedAssistantProvider([claude, codex], fixed_order=True)

    reply = await router.respond(_request("Explain Docker containers"))

    assert reply.provider == "codex"
    assert reply.text == "answered by codex"
    assert claude.calls == 1
    assert codex.calls == 1


async def test_respond_raises_only_when_every_provider_is_empty_or_fails():
    claude = _Provider("claude_code", empty=True)
    codex = _Provider("codex", fail=True)
    router = RoutedAssistantProvider([claude, codex], fixed_order=True)

    with pytest.raises(AssistantProviderError):
        await router.respond(_request("Explain Docker containers"))


async def test_respond_stream_falls_over_on_empty_completion_without_error():
    """Same gap as respond(), but in the streaming adapter path that
    normal-conversation voice/text turns actually use."""
    claude = _StreamProvider("claude_code")
    codex = _Provider("codex")
    router = RoutedAssistantProvider([claude, codex], fixed_order=True)

    events = [event async for event in router.respond_stream(_request("Explain Docker containers"))]

    complete = [e for e in events if e.get("type") == "complete"]
    assert complete
    assert complete[-1]["text"] == "answered by codex"
    assert complete[-1]["provider"] == "codex"
    assert claude.calls == 1
    assert codex.calls == 1


async def test_respond_stream_does_not_retry_provider_after_partial_content_sent():
    """If a provider already streamed real content before failing, the
    router must surface the error rather than silently starting a second,
    unrelated answer from a different provider after it."""

    class _PartialThenFailProvider(AssistantProvider):
        name = "flaky_partial"
        is_available = True

        async def respond(self, request: AssistantRequest) -> AssistantReply:
            raise AssertionError("respond() should not be used when respond_stream exists")

        async def respond_stream(self, request: AssistantRequest):
            yield {"type": "delta", "text": "Docker containers are"}
            raise AssistantProviderError("connection dropped mid-stream")

    flaky = _PartialThenFailProvider()
    codex = _Provider("codex")
    router = RoutedAssistantProvider([flaky, codex], fixed_order=True)

    with pytest.raises(AssistantProviderError):
        async for _event in router.respond_stream(_request("Explain Docker containers")):
            pass

    assert codex.calls == 0
