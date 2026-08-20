from __future__ import annotations

import aiosqlite
import pytest

from app.latency_store import LatencyTraceStore, RequestTrace
from storage.migrator import run_migrations


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "latency_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


async def test_record_and_recent_round_trip(conn):
    store = LatencyTraceStore(conn)
    await store.record(
        RequestTrace(
            request_id="r1",
            kind="chart_analysis",
            conversation_id="c1",
            provider_id="claude_code",
            started_at="2026-08-20T00:00:00Z",
            total_ms=1200.0,
        )
    )

    rows = await store.recent("chart_analysis")
    assert len(rows) == 1
    assert rows[0]["request_id"] == "r1"
    assert rows[0]["total_ms"] == 1200.0


async def test_recent_filters_by_kind(conn):
    store = LatencyTraceStore(conn)
    await store.record(RequestTrace(request_id="r1", kind="chart_analysis", started_at="t", total_ms=100.0))
    await store.record(RequestTrace(request_id="r2", kind="assistant_text", started_at="t", total_ms=200.0))

    chart_rows = await store.recent("chart_analysis")
    text_rows = await store.recent("assistant_text")
    assert [r["request_id"] for r in chart_rows] == ["r1"]
    assert [r["request_id"] for r in text_rows] == ["r2"]


async def test_percentiles_computed_over_recorded_totals(conn):
    store = LatencyTraceStore(conn)
    for i, total in enumerate([10_000, 15_000, 20_000, 25_000, 31_000]):
        await store.record(
            RequestTrace(
                request_id=f"r{i}", kind="chart_analysis", started_at="t", total_ms=float(total)
            )
        )

    stats = await store.percentiles("chart_analysis")
    assert stats["sample_size"] == 5
    assert stats["error_count"] == 0
    assert stats["max_ms"] == 31_000
    assert stats["p50_ms"] == 20_000
    assert stats["p50_ms"] <= stats["p90_ms"] <= stats["p95_ms"] <= stats["max_ms"]


async def test_percentiles_with_no_data_returns_nulls(conn):
    store = LatencyTraceStore(conn)
    stats = await store.percentiles("chart_analysis")
    assert stats["sample_size"] == 0
    assert stats["p50_ms"] is None
    assert stats["max_ms"] is None


async def test_percentiles_counts_errors_separately_from_sample_size(conn):
    store = LatencyTraceStore(conn)
    await store.record(
        RequestTrace(request_id="ok", kind="chart_analysis", started_at="t", total_ms=5000.0)
    )
    await store.record(
        RequestTrace(
            request_id="failed",
            kind="chart_analysis",
            started_at="t",
            total_ms=200.0,
            error="provider timeout",
        )
    )

    stats = await store.percentiles("chart_analysis")
    assert stats["sample_size"] == 2
    assert stats["error_count"] == 1


async def test_record_upserts_on_same_request_id(conn):
    store = LatencyTraceStore(conn)
    await store.record(RequestTrace(request_id="r1", kind="chart_analysis", started_at="t", total_ms=100.0))
    await store.record(RequestTrace(request_id="r1", kind="chart_analysis", started_at="t", total_ms=999.0))

    rows = await store.recent("chart_analysis")
    assert len(rows) == 1
    assert rows[0]["total_ms"] == 999.0
