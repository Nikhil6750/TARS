from __future__ import annotations

import aiosqlite
import pytest

from app.latency_store import LatencyTraceStore, RequestTrace
from assistant.provider_health import ProviderHealthTracker
from storage.migrator import run_migrations


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "provider_health_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


async def _record(store, *, provider_id, total_ms, error=None, request_id=None):
    await store.record(
        RequestTrace(
            request_id=request_id or f"{provider_id}-{total_ms}-{error or 'ok'}",
            kind="assistant_text",
            provider_id=provider_id,
            started_at="2026-08-21T00:00:00Z",
            total_ms=total_ms,
            error=error,
        )
    )


async def test_health_for_unknown_provider_reports_zero_sample_size(conn):
    tracker = ProviderHealthTracker(LatencyTraceStore(conn))
    health = await tracker.health_for("claude_code")
    assert health.sample_size == 0
    assert health.success_rate == 0.0
    assert health.p50_ms is None


async def test_health_for_computes_success_rate_and_percentiles(conn):
    store = LatencyTraceStore(conn)
    for total in (1000, 1200, 1500, 2000, 5000):
        await _record(store, provider_id="claude_code", total_ms=total)
    await _record(store, provider_id="claude_code", total_ms=200, error="timeout")

    tracker = ProviderHealthTracker(store)
    health = await tracker.health_for("claude_code")

    assert health.sample_size == 6
    assert health.error_count == 1
    assert health.success_rate == pytest.approx(5 / 6)
    assert health.last_error == "timeout"
    assert health.p50_ms is not None
    assert health.p50_ms <= health.p90_ms <= health.p95_ms <= health.max_ms


async def test_health_for_only_counts_the_requested_provider(conn):
    store = LatencyTraceStore(conn)
    await _record(store, provider_id="claude_code", total_ms=1000)
    await _record(store, provider_id="codex", total_ms=500)

    tracker = ProviderHealthTracker(store)
    claude_health = await tracker.health_for("claude_code")
    codex_health = await tracker.health_for("codex")

    assert claude_health.sample_size == 1
    assert codex_health.sample_size == 1


async def test_healthiest_prefers_higher_success_rate(conn):
    store = LatencyTraceStore(conn)
    # claude_code: 1 success, 1 failure -> 50%
    await _record(store, provider_id="claude_code", total_ms=1000)
    await _record(store, provider_id="claude_code", total_ms=1000, error="boom")
    # codex: 2 successes -> 100%, but slower
    await _record(store, provider_id="codex", total_ms=3000)
    await _record(store, provider_id="codex", total_ms=3000)

    tracker = ProviderHealthTracker(store)
    winner = await tracker.healthiest(["claude_code", "codex"])
    assert winner == "codex"


async def test_healthiest_prefers_lower_p50_when_success_rates_tie(conn):
    store = LatencyTraceStore(conn)
    for total in (1000, 1000):
        await _record(store, provider_id="fast_provider", total_ms=total)
    for total in (5000, 5000):
        await _record(store, provider_id="slow_provider", total_ms=total)

    tracker = ProviderHealthTracker(store)
    winner = await tracker.healthiest(["fast_provider", "slow_provider"])
    assert winner == "fast_provider"


async def test_healthiest_returns_none_when_no_candidate_has_data(conn):
    tracker = ProviderHealthTracker(LatencyTraceStore(conn))
    winner = await tracker.healthiest(["claude_code", "codex"])
    assert winner is None


async def test_healthiest_ignores_a_candidate_with_no_data_when_another_has_some(conn):
    store = LatencyTraceStore(conn)
    await _record(store, provider_id="claude_code", total_ms=1000)

    tracker = ProviderHealthTracker(store)
    winner = await tracker.healthiest(["claude_code", "never_used_provider"])
    assert winner == "claude_code"
