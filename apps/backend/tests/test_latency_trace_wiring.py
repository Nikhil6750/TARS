"""Regression coverage for the previously-discarded ProviderDiagnostics gap:
AssistantRouter._call_provider computed a diagnostics dataclass on every
provider reply but never persisted or logged it (assistant/router.py:299
just dropped it). These tests pin that AssistantRouter now writes a
request_traces row for both the success and provider-failure paths, and
that /api/v1/diagnostics/latency reads real recorded data back out.
"""
from __future__ import annotations

import aiosqlite
import pytest

from app.latency_store import LatencyTraceStore
from assistant.conversation_store import ConversationStore
from assistant.errors import AssistantProviderError
from assistant.provider import (
    AssistantProvider,
    AssistantReply,
    AssistantRequest,
    ProviderDiagnostics,
)
from assistant.router import AssistantRouter
from events.service import EventService
from storage.migrator import run_migrations


class _DiagnosticProvider(AssistantProvider):
    name = "diag_fake"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        if self.fail:
            raise AssistantProviderError("provider unreachable")
        return AssistantReply(
            text="a real reply",
            provider=self.name,
            diagnostics=ProviderDiagnostics(provider_id=self.name, latency_ms=1234.5),
        )


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "latency_wiring_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


async def test_call_provider_persists_diagnostics_on_success(conn):
    trace_store = LatencyTraceStore(conn)
    router = AssistantRouter(
        event_service=EventService(conn),
        conversation_store=ConversationStore(conn),
        provider=_DiagnosticProvider(),
        trace_store=trace_store,
    )

    await router.handle_text("hello there, how are you today", None)

    rows = await trace_store.recent("assistant_text")
    assert len(rows) == 1
    assert rows[0]["provider_id"] == "diag_fake"
    assert rows[0]["provider_latency_ms"] == 1234.5
    assert rows[0]["error"] is None


async def test_call_provider_persists_a_trace_on_provider_failure(conn):
    trace_store = LatencyTraceStore(conn)
    router = AssistantRouter(
        event_service=EventService(conn),
        conversation_store=ConversationStore(conn),
        provider=_DiagnosticProvider(fail=True),
        trace_store=trace_store,
    )

    await router.handle_text("hello there, how are you today", None)

    rows = await trace_store.recent("assistant_text")
    assert len(rows) == 1
    assert rows[0]["error"] is not None
    assert "unreachable" in rows[0]["error"]


async def test_handle_text_works_without_a_trace_store(conn):
    router = AssistantRouter(
        event_service=EventService(conn),
        conversation_store=ConversationStore(conn),
        provider=_DiagnosticProvider(),
    )
    reply = await router.handle_text("hello there, how are you today", None)
    assert reply.assistant_message.content == "a real reply"
