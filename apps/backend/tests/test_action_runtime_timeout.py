"""ActionRuntime._resolve_execution_timeout -- a skill can opt one of its own
actions out of the runtime's generic dispatch timeout when that action's own
bounded verification contract can legitimately take longer (reproduced live:
tradingview.set_timeframe/set_symbol being cut off by the generic 30s default
mid-vision-verification; see skills/tradingview_control.py). Every skill that
doesn't override execution_timeout_for is completely unaffected -- the
generic default still applies to them, as covered by test_action_runtime.py.
"""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from actions.registry import SkillRegistry
from actions.runtime import ActionRuntime
from actions.store import ActionStore
from app.action_contracts import (
    ActionRequest,
    ActionResult,
    ActionStatus,
    BaseSkill,
    RiskLevel,
)
from storage.migrator import run_migrations


class _SlowSkill(BaseSkill):
    name = "slow"
    capabilities: tuple[str, ...] = ("act",)

    def __init__(self, *, sleep_seconds: float, timeout_hint: float | None) -> None:
        self._sleep_seconds = sleep_seconds
        self._timeout_hint = timeout_hint

    def classify_risk(self, action: str, arguments: dict) -> RiskLevel:
        return RiskLevel.READ_ONLY

    async def validate(self, action: str, arguments: dict) -> None:
        return None

    def execution_timeout_for(self, action: str) -> float | None:
        return self._timeout_hint

    async def execute(self, request: ActionRequest) -> ActionResult:
        await asyncio.sleep(self._sleep_seconds)
        return self._result(request, ActionStatus.SUCCEEDED, "done", risk_level=RiskLevel.READ_ONLY)


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "runtime_timeout_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


async def _make_runtime(conn, skill: BaseSkill, *, execution_timeout: float) -> ActionRuntime:
    store = ActionStore(conn)
    await store.initialize()
    registry = SkillRegistry([skill])
    return ActionRuntime(store, registry, execution_timeout=execution_timeout)


def _request() -> ActionRequest:
    from datetime import UTC, datetime
    from uuid import uuid4

    return ActionRequest(
        schema_version="1.0.0", id=uuid4(), skill="slow", action="act",
        arguments={}, source="api", requested_at=datetime.now(UTC),
    )


async def test_skill_timeout_hint_lets_an_action_outlive_the_generic_default(conn):
    # Generic default is 0.05s; the action sleeps 0.2s -- would time out
    # without a hint, but the skill's hint (10s) comfortably covers it.
    skill = _SlowSkill(sleep_seconds=0.2, timeout_hint=10.0)
    runtime = await _make_runtime(conn, skill, execution_timeout=0.05)

    result = await runtime.submit(_request())

    assert result.status == ActionStatus.SUCCEEDED


async def test_no_timeout_hint_still_uses_the_generic_default(conn):
    skill = _SlowSkill(sleep_seconds=0.2, timeout_hint=None)
    runtime = await _make_runtime(conn, skill, execution_timeout=0.05)

    result = await runtime.submit(_request())

    assert result.status == ActionStatus.FAILED
    assert result.error == "Execution timeout"


async def test_caller_supplied_override_wins_over_the_skill_hint(conn):
    # The skill would happily allow 10s, but the caller (e.g. a plan runtime
    # budgeting its own deadline) explicitly asks for a much tighter bound.
    skill = _SlowSkill(sleep_seconds=0.2, timeout_hint=10.0)
    runtime = await _make_runtime(conn, skill, execution_timeout=30.0)

    result = await runtime.submit(_request(), execution_timeout=0.05)

    assert result.status == ActionStatus.FAILED
    assert result.error == "Execution timeout"
