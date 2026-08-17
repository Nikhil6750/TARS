from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import aiosqlite

from actions.plan_models import (
    ActionPlan,
    PlanExecution,
    PlanStatus,
    StepStatus,
    StructuredObservation,
    VerificationRecord,
)
from actions.safety import redact_sensitive
from app.action_contracts import RiskLevel


class DuplicatePlanError(RuntimeError):
    pass


class DuplicateObservationError(RuntimeError):
    pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS action_plans (
    plan_id TEXT PRIMARY KEY,
    plan_json TEXT NOT NULL,
    status TEXT NOT NULL,
    current_step_index INTEGER NOT NULL DEFAULT 0,
    step_statuses_json TEXT NOT NULL,
    effective_risks_json TEXT NOT NULL,
    attempts_json TEXT NOT NULL DEFAULT '{}',
    reobservations_json TEXT NOT NULL DEFAULT '{}',
    active_request_id TEXT,
    verification_json TEXT,
    latest_sensitive_json TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    deadline TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_plan_audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    step_id TEXT,
    request_id TEXT,
    event TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (plan_id) REFERENCES action_plans(plan_id)
);

CREATE TABLE IF NOT EXISTS action_plan_observations (
    observation_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    source TEXT NOT NULL,
    observation_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (plan_id) REFERENCES action_plans(plan_id)
);

CREATE INDEX IF NOT EXISTS idx_action_plan_audit_plan_sequence
ON action_plan_audit(plan_id, sequence);
"""


class PlanStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def initialize(self) -> None:
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def insert(
        self,
        plan: ActionPlan,
        *,
        effective_risks: dict[str, str],
        deadline: datetime,
        now: datetime,
    ) -> None:
        statuses = {str(step.step_id): step.status.value for step in plan.steps}
        try:
            await self._conn.execute(
                """
                INSERT INTO action_plans (
                    plan_id, plan_json, status, step_statuses_json,
                    effective_risks_json, deadline, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(plan.plan_id),
                    plan.model_dump_json(),
                    plan.status.value,
                    json.dumps(statuses, separators=(",", ":")),
                    json.dumps(effective_risks, separators=(",", ":")),
                    deadline.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            await self._conn.commit()
        except aiosqlite.IntegrityError as exc:
            await self._conn.rollback()
            raise DuplicatePlanError(f"Action plan {plan.plan_id} already exists") from exc

    async def get(self, plan_id: UUID | str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM action_plans WHERE plan_id = ?", (str(plan_id),)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update(self, plan_id: UUID | str, now: datetime, **changes: Any) -> None:
        allowed = {
            "status",
            "current_step_index",
            "step_statuses_json",
            "attempts_json",
            "reobservations_json",
            "active_request_id",
            "verification_json",
            "latest_sensitive_json",
            "error",
        }
        if not changes or set(changes) - allowed:
            raise ValueError("Invalid plan state update")
        assignments = ", ".join(f"{name} = ?" for name in changes)
        values = [changes[name] for name in changes]
        await self._conn.execute(
            f"UPDATE action_plans SET {assignments}, updated_at = ? WHERE plan_id = ?",
            (*values, now.isoformat(), str(plan_id)),
        )
        await self._conn.commit()

    async def append_audit(
        self,
        plan_id: UUID | str,
        event: str,
        now: datetime,
        *,
        step_id: UUID | str | None = None,
        request_id: UUID | str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        safe_details = redact_sensitive(details or {})
        await self._conn.execute(
            """
            INSERT INTO action_plan_audit (
                plan_id, step_id, request_id, event, details_json, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(plan_id),
                str(step_id) if step_id else None,
                str(request_id) if request_id else None,
                event,
                json.dumps(safe_details, separators=(",", ":"), default=str),
                now.isoformat(),
            ),
        )
        await self._conn.commit()

    async def insert_observation(
        self, observation: StructuredObservation, now: datetime
    ) -> None:
        safe = redact_sensitive(observation.model_dump(mode="json"))
        try:
            await self._conn.execute(
                """
                INSERT INTO action_plan_observations (
                    observation_id, plan_id, step_id, request_id, source,
                    observation_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(observation.observation_id),
                    str(observation.plan_id),
                    str(observation.step_id),
                    str(observation.request_id),
                    observation.source.value,
                    json.dumps(safe, separators=(",", ":"), default=str),
                    now.isoformat(),
                ),
            )
            await self._conn.commit()
        except aiosqlite.IntegrityError as exc:
            await self._conn.rollback()
            raise DuplicateObservationError("Observation has already been consumed") from exc

    async def list_audit(self, plan_id: UUID | str) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            """
            SELECT sequence, plan_id, step_id, request_id, event, details_json, recorded_at
            FROM action_plan_audit WHERE plan_id = ? ORDER BY sequence ASC
            """,
            (str(plan_id),),
        )
        rows = await cursor.fetchall()
        return [
            {
                "sequence": row["sequence"],
                "plan_id": row["plan_id"],
                "step_id": row["step_id"],
                "request_id": row["request_id"],
                "event": row["event"],
                "details": json.loads(row["details_json"]),
                "recorded_at": row["recorded_at"],
            }
            for row in rows
        ]

    @staticmethod
    def execution_from_record(record: dict[str, Any]) -> PlanExecution:
        plan = ActionPlan.model_validate_json(record["plan_json"])
        statuses = json.loads(record["step_statuses_json"])
        risks = json.loads(record["effective_risks_json"])
        steps = [
            step.model_copy(
                update={
                    "status": StepStatus(statuses[str(step.step_id)]),
                    "risk_level": RiskLevel(risks[str(step.step_id)]),
                }
            )
            for step in plan.steps
        ]
        plan = plan.model_copy(
            update={"status": PlanStatus(record["status"]), "steps": steps}
        )
        index = int(record["current_step_index"])
        current = steps[index].step_id if index < len(steps) else None
        verification = (
            VerificationRecord.model_validate_json(record["verification_json"])
            if record["verification_json"]
            else None
        )
        pending = None
        if current is not None and record["status"] == PlanStatus.WAITING_CONFIRMATION.value:
            step = steps[index]
            pending = {
                "step_id": str(step.step_id),
                "skill": step.skill,
                "action": step.action,
                "arguments": redact_sensitive(step.arguments),
                "risk_level": step.risk_level,
            }
        return PlanExecution(
            plan=plan,
            current_step_id=current,
            active_request_id=record["active_request_id"],
            pending_operation=pending,
            verification=verification,
            error=record["error"],
        )
