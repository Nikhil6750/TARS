"""Persistence for HotChartState (see assistant/hot_chart_state.py and
storage/migrations/0006_hot_chart_state.sql). Same shared-connection
`*Store` pattern as `app.latency_store.LatencyTraceStore` and every other
service in this backend -- no separate pool, no ORM.

Purely additive in Phase B: nothing in the live request path calls this
yet. Phase C's BackgroundChartWatcher is the first real writer; Phase D's
fast path is the first real reader.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import aiosqlite

from assistant.chart_analysis import ChartAnalysisResult
from assistant.hot_chart_state import ChartIdentity, HotChartState


def _empty_to_none(value: str) -> str | None:
    return value or None


def _none_to_empty(value: str | None) -> str:
    return value or ""


class HotChartStateStore:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    async def upsert(self, state: HotChartState) -> HotChartState:
        """Writes state, incrementing `version` if a row for this exact
        identity already exists (so a caller can tell "this is the third
        refresh for this identity" from a fresh read, without needing a
        separate history table)."""
        existing = await self.get(state.identity)
        version = (existing.version + 1) if existing else 1
        state.version = version

        await self._conn.execute(
            """
            INSERT INTO hot_chart_state (
                chart_window_id, symbol, timeframe, screenshot_hash,
                source, observed_at, analyzed_at, version, analysis_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (chart_window_id, symbol, timeframe) DO UPDATE SET
                screenshot_hash = excluded.screenshot_hash,
                source = excluded.source,
                observed_at = excluded.observed_at,
                analyzed_at = excluded.analyzed_at,
                version = excluded.version,
                analysis_json = excluded.analysis_json
            """,
            (
                state.identity.chart_window_id,
                _none_to_empty(state.identity.symbol),
                _none_to_empty(state.identity.timeframe),
                state.screenshot_hash,
                state.source,
                state.observed_at,
                state.analyzed_at,
                state.version,
                json.dumps(asdict(state.analysis)),
            ),
        )
        await self._conn.commit()
        return state

    async def get(self, identity: ChartIdentity) -> HotChartState | None:
        """Exact-identity lookup only -- chart_window_id, symbol, and
        timeframe must all match. There is no fuzzy/partial match; a
        caller wanting "closest available state" must query differently
        (not provided here, since that would invite exactly the
        cache-correctness bug Part 19 warns against)."""
        cursor = await self._conn.execute(
            """
            SELECT * FROM hot_chart_state
            WHERE chart_window_id = ? AND symbol = ? AND timeframe = ?
            """,
            (
                identity.chart_window_id,
                _none_to_empty(identity.symbol),
                _none_to_empty(identity.timeframe),
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_state(row)

    async def get_latest_for_window(self, chart_window_id: str) -> HotChartState | None:
        """Most-recently-analyzed row for this window, regardless of
        symbol/timeframe -- used only for the watcher's own "do we already
        have something fresh enough for this window" pre-check before
        spending a vision call (assistant/chart_watch.py). Never used to
        answer a user's chart-analysis request directly; that path (Phase
        D) always requires the caller to supply the exact identity it
        needs via `get()`, per Part 19's cache-correctness rule."""
        cursor = await self._conn.execute(
            """
            SELECT * FROM hot_chart_state
            WHERE chart_window_id = ?
            ORDER BY analyzed_at DESC
            LIMIT 1
            """,
            (chart_window_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_state(row)

    async def delete(self, identity: ChartIdentity) -> None:
        await self._conn.execute(
            """
            DELETE FROM hot_chart_state
            WHERE chart_window_id = ? AND symbol = ? AND timeframe = ?
            """,
            (
                identity.chart_window_id,
                _none_to_empty(identity.symbol),
                _none_to_empty(identity.timeframe),
            ),
        )
        await self._conn.commit()


def _row_to_state(row: Any) -> HotChartState:
    payload = json.loads(row["analysis_json"])
    analysis = ChartAnalysisResult(**payload)
    identity = ChartIdentity(
        chart_window_id=row["chart_window_id"],
        symbol=_empty_to_none(row["symbol"]),
        timeframe=_empty_to_none(row["timeframe"]),
    )
    return HotChartState(
        identity=identity,
        analysis=analysis,
        screenshot_hash=row["screenshot_hash"],
        source=row["source"],
        observed_at=row["observed_at"],
        analyzed_at=row["analyzed_at"],
        version=row["version"],
    )
