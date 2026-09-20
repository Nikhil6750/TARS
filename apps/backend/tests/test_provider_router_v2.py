from __future__ import annotations

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
    classify_provider_task,
)
from storage.migrator import run_migrations


class _Provider(AssistantProvider):
    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.calls = 0
        self.is_available = True

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        self.calls += 1
        if self.fail:
            raise AssistantProviderError("internal executable failure")
        return AssistantReply(
            text=f"answered by {self.name}",
            provider=self.name,
            diagnostics=ProviderDiagnostics(provider_id=self.name),
        )


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
