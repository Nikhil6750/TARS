"""Append-only-style audit trail over `agent_runs`.

Unlike `actions/store.py`'s `ActionStore` / `actions/plan_store.py`'s
`PlanStore`, this store does not create its own table: `agent_runs` is
created by `storage/migrations/0002_tars_core_memory_agents.sql` and applied
once by the migration runner (see AGENTS.md build instructions — this
package was told not to introduce a second migration for a table that
already exists). `initialize()` is kept anyway, as a no-op, purely for
interface symmetry with ActionStore/PlanStore so callers can await
`store.initialize()` uniformly during startup wiring without special-casing
this one store.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import aiosqlite

from agents.models import AgentMode, AgentRunStatus


class AgentRunStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def initialize(self) -> None:
        """No-op — `agent_runs` already exists via the applied migration."""
        return None

    async def start_run(
        self,
        run_id: str,
        agent_name: str,
        mode: AgentMode,
        trigger: str,
        now: datetime,
    ) -> None:
        await self._conn.execute(
            """
            INSERT INTO agent_runs (
                run_id, agent_name, mode, status, trigger, started_at, iterations
            ) VALUES (?, ?, ?, ?, ?, ?, 0)
            """,
            (
                run_id,
                agent_name,
                mode.value,
                AgentRunStatus.RUNNING.value,
                trigger,
                now.isoformat(),
            ),
        )
        await self._conn.commit()

    async def finish_run(
        self,
        run_id: str,
        status: AgentRunStatus,
        now: datetime,
        *,
        iterations: int,
        summary: str | None,
        error: str | None,
    ) -> None:
        await self._conn.execute(
            """
            UPDATE agent_runs
            SET status = ?, finished_at = ?, iterations = ?, summary = ?, error = ?
            WHERE run_id = ?
            """,
            (status.value, now.isoformat(), iterations, summary, error, run_id),
        )
        await self._conn.commit()

    async def list_recent(
        self, agent_name: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        if agent_name:
            cursor = await self._conn.execute(
                """
                SELECT * FROM agent_runs WHERE agent_name = ?
                ORDER BY started_at DESC LIMIT ?
                """,
                (agent_name, limit),
            )
        else:
            cursor = await self._conn.execute(
                "SELECT * FROM agent_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
