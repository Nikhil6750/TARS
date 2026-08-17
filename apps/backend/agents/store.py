from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import aiosqlite

from actions.safety import redact_sensitive
from agents.contracts import AgentJob, AgentRun, AgentStatus
from agents.errors import AgentConflictError, AgentJobNotFoundError, DuplicateJobError
from agents.safety import assert_secret_free

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_jobs (
    job_id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    job_json TEXT NOT NULL,
    status TEXT NOT NULL,
    iteration INTEGER NOT NULL DEFAULT 0,
    cycle INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL,
    next_run_at TEXT,
    pending_action_id TEXT,
    last_action_json TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    event TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES agent_jobs(job_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_jobs_due ON agent_jobs(status, next_run_at);
CREATE INDEX IF NOT EXISTS idx_agent_audit_job_sequence
ON agent_audit(job_id, sequence);
"""


class AgentStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def initialize(self) -> None:
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def insert(self, job: AgentJob, status: AgentStatus, now: datetime) -> AgentRun:
        payload = job.model_dump(mode="json")
        assert_secret_free(payload, label="agent job")
        next_run = job.scheduled_for if status == AgentStatus.SCHEDULED else None
        summary = "Agent job accepted."
        try:
            await self._conn.execute(
                """
                INSERT INTO agent_jobs (
                    job_id, dedupe_key, job_json, status, summary, next_run_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(job.job_id),
                    job.dedupe_key,
                    _dump(payload),
                    status.value,
                    summary,
                    next_run.isoformat() if next_run else None,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            await self._conn.commit()
        except aiosqlite.IntegrityError as exc:
            await self._conn.rollback()
            raise DuplicateJobError(
                f"agent job id or dedupe key already exists: {job.job_id}"
            ) from exc
        await self.append_audit(job.job_id, "JOB_ACCEPTED", status, summary, now)
        return await self.get_run(job.job_id)

    async def get_record(self, job_id: UUID | str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM agent_jobs WHERE job_id = ?", (str(job_id),)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def require_record(self, job_id: UUID | str) -> dict[str, Any]:
        record = await self.get_record(job_id)
        if record is None:
            raise AgentJobNotFoundError(f"agent job {job_id} was not found")
        return record

    async def get_job(self, job_id: UUID | str) -> AgentJob:
        record = await self.require_record(job_id)
        return AgentJob.model_validate_json(record["job_json"])

    async def get_run(self, job_id: UUID | str) -> AgentRun:
        return _run(await self.require_record(job_id))

    async def claim(
        self, job_id: UUID, now: datetime, *, increment_cycle: bool = True
    ) -> dict[str, Any]:
        cursor = await self._conn.execute(
            """
            UPDATE agent_jobs
            SET status = ?, cycle = cycle + ?, summary = ?, error = NULL,
                next_run_at = NULL, updated_at = ?
            WHERE job_id = ? AND status IN (?, ?, ?, ?, ?)
              AND cancel_requested = 0
            """,
            (
                AgentStatus.RUNNING.value,
                int(increment_cycle),
                "Agent run started.",
                now.isoformat(),
                str(job_id),
                AgentStatus.READY.value,
                AgentStatus.SCHEDULED.value,
                AgentStatus.PAUSED.value,
                AgentStatus.RECOVERY_REQUIRED.value,
                AgentStatus.WAITING_CONFIRMATION.value,
            ),
        )
        await self._conn.commit()
        if cursor.rowcount != 1:
            raise AgentConflictError("agent job is not runnable or is already running")
        return await self.require_record(job_id)

    async def update(
        self,
        job_id: UUID,
        now: datetime,
        *,
        status: AgentStatus | None = None,
        iteration: int | None = None,
        summary: str | None = None,
        next_run_at: datetime | None = None,
        pending_action_id: UUID | None = None,
        last_action: dict[str, Any] | None = None,
        cancel_requested: bool | None = None,
        error: str | None = None,
        clear_pending_action: bool = False,
    ) -> AgentRun:
        record = await self.require_record(job_id)
        values = {
            "status": status.value if status else record["status"],
            "iteration": iteration if iteration is not None else record["iteration"],
            "summary": (
                str(redact_sensitive(summary))
                if summary is not None
                else record["summary"]
            ),
            "next_run_at": (
                next_run_at.isoformat()
                if next_run_at is not None
                else record["next_run_at"]
            ),
            "pending_action_id": (
                None
                if clear_pending_action
                else str(pending_action_id)
                if pending_action_id is not None
                else record["pending_action_id"]
            ),
            "last_action_json": (
                _dump(redact_sensitive(last_action))
                if last_action is not None
                else record["last_action_json"]
            ),
            "cancel_requested": (
                int(cancel_requested)
                if cancel_requested is not None
                else record["cancel_requested"]
            ),
            "error": str(redact_sensitive(error)) if error is not None else None,
        }
        await self._conn.execute(
            """
            UPDATE agent_jobs SET status = :status, iteration = :iteration,
                summary = :summary, next_run_at = :next_run_at,
                pending_action_id = :pending_action_id,
                last_action_json = :last_action_json,
                cancel_requested = :cancel_requested, error = :error,
                updated_at = :updated_at
            WHERE job_id = :job_id
            """,
            {**values, "updated_at": now.isoformat(), "job_id": str(job_id)},
        )
        await self._conn.commit()
        return await self.get_run(job_id)

    async def request_cancel(self, job_id: UUID, now: datetime) -> AgentRun:
        await self.require_record(job_id)
        await self._conn.execute(
            """
            UPDATE agent_jobs
            SET status = CASE WHEN status = ? THEN ? ELSE ? END,
                summary = CASE WHEN status = ? THEN ? ELSE ? END,
                cancel_requested = 1, error = NULL, updated_at = ?
            WHERE job_id = ? AND status NOT IN (?, ?, ?, ?, ?)
            """,
            (
                AgentStatus.RUNNING.value,
                AgentStatus.CANCELLING.value,
                AgentStatus.CANCELLED.value,
                AgentStatus.RUNNING.value,
                "Cancellation requested.",
                "Cancelled.",
                now.isoformat(),
                str(job_id),
                AgentStatus.CANCELLED.value,
                AgentStatus.SUCCEEDED.value,
                AgentStatus.FAILED.value,
                AgentStatus.TIMED_OUT.value,
                AgentStatus.EXHAUSTED.value,
            ),
        )
        await self._conn.commit()
        run = await self.get_run(job_id)
        if run.status in {
            AgentStatus.CANCELLED,
            AgentStatus.SUCCEEDED,
            AgentStatus.FAILED,
            AgentStatus.TIMED_OUT,
            AgentStatus.EXHAUSTED,
        }:
            return run
        await self.append_audit(
            job_id, "CANCEL_REQUESTED", run.status, run.summary, now
        )
        return run

    async def is_cancel_requested(self, job_id: UUID) -> bool:
        return bool((await self.require_record(job_id))["cancel_requested"])

    async def due_job_ids(self, now: datetime, limit: int = 100) -> list[UUID]:
        cursor = await self._conn.execute(
            """
            SELECT job_id FROM agent_jobs
            WHERE status IN (?, ?) AND next_run_at IS NOT NULL AND next_run_at <= ?
              AND cancel_requested = 0
            ORDER BY next_run_at ASC LIMIT ?
            """,
            (
                AgentStatus.SCHEDULED.value,
                AgentStatus.PAUSED.value,
                now.isoformat(),
                max(1, min(limit, 500)),
            ),
        )
        return [UUID(row["job_id"]) for row in await cursor.fetchall()]

    async def mark_interrupted(self, now: datetime) -> list[UUID]:
        cursor = await self._conn.execute(
            "SELECT job_id FROM agent_jobs WHERE status IN (?, ?)",
            (AgentStatus.RUNNING.value, AgentStatus.CANCELLING.value),
        )
        ids = [UUID(row["job_id"]) for row in await cursor.fetchall()]
        for job_id in ids:
            await self.update(
                job_id,
                now,
                status=AgentStatus.RECOVERY_REQUIRED,
                summary="Previous process stopped before the run completed.",
                cancel_requested=False,
                error="Interrupted run requires explicit recovery.",
            )
            await self.append_audit(
                job_id,
                "RECOVERY_REQUIRED",
                AgentStatus.RECOVERY_REQUIRED,
                "Interrupted run detected; no success was inferred.",
                now,
            )
        return ids

    async def append_audit(
        self,
        job_id: UUID,
        event: str,
        status: AgentStatus,
        summary: str,
        now: datetime,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        await self._conn.execute(
            """
            INSERT INTO agent_audit (job_id, event, status, summary, details_json, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(job_id),
                event,
                status.value,
                str(redact_sensitive(summary)),
                _dump(redact_sensitive(details or {})),
                now.isoformat(),
            ),
        )
        await self._conn.commit()

    async def list_audit(self, job_id: UUID) -> list[dict[str, Any]]:
        await self.require_record(job_id)
        cursor = await self._conn.execute(
            """
            SELECT sequence, job_id, event, status, summary, details_json, recorded_at
            FROM agent_audit WHERE job_id = ? ORDER BY sequence ASC
            """,
            (str(job_id),),
        )
        return [
            {
                "sequence": row["sequence"],
                "job_id": row["job_id"],
                "event": row["event"],
                "status": row["status"],
                "summary": row["summary"],
                "details": json.loads(row["details_json"]),
                "recorded_at": row["recorded_at"],
            }
            for row in await cursor.fetchall()
        ]


def _run(record: dict[str, Any]) -> AgentRun:
    return AgentRun(
        job_id=UUID(record["job_id"]),
        status=AgentStatus(record["status"]),
        iteration=int(record["iteration"]),
        cycle=int(record["cycle"]),
        summary=record["summary"],
        next_run_at=(
            datetime.fromisoformat(record["next_run_at"]) if record["next_run_at"] else None
        ),
        pending_action_id=(
            UUID(record["pending_action_id"]) if record["pending_action_id"] else None
        ),
        error=record["error"],
        updated_at=datetime.fromisoformat(record["updated_at"]),
    )


def _dump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)
