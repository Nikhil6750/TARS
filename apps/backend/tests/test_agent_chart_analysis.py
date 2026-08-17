from __future__ import annotations

import aiosqlite
import pytest

from actions.registry import SkillRegistry
from actions.runtime import ActionRuntime
from actions.store import ActionStore
from agents.chart_analysis_agent import ChartAnalysisAgent
from agents.models import AgentRunStatus
from app.action_contracts import (
    ActionRequest,
    ActionResult,
    ActionStatus,
    BaseSkill,
    RiskLevel,
)
from memory.service import KIND_TRADING_OBSERVATION, MemoryService
from storage.migrator import run_migrations


class FakeTradingSkill(BaseSkill):
    """A stand-in for the real `trading` skill's `analyze_active_chart`
    action, controllable per test -- mirrors test_plan_runtime.py's
    PlanSkill fake rather than mocking ActionRuntime/ActionStore
    internals."""

    name = "trading"
    capabilities: tuple[str, ...] = ("analyze_active_chart",)

    def __init__(self, *, outcome: dict | None = None) -> None:
        self._outcome = outcome or {}
        self.calls: list[dict] = []

    def classify_risk(self, action: str, arguments: dict) -> RiskLevel:
        return RiskLevel.READ_ONLY

    async def validate(self, action: str, arguments: dict) -> None:
        return None

    async def execute(self, request: ActionRequest) -> ActionResult:
        self.calls.append(request.arguments)
        return self._result(
            request,
            self._outcome.get("status", ActionStatus.SUCCEEDED),
            self._outcome.get("summary", "Chart analyzed."),
            risk_level=RiskLevel.READ_ONLY,
            data=self._outcome.get("data") or {},
            error=self._outcome.get("error"),
        )


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "chart_analysis_agent_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


@pytest.fixture
async def memory(conn, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    return MemoryService(conn, vault_path=str(vault), sqlite_vec_enabled=False)


async def _make_runtime(conn, skill: FakeTradingSkill) -> ActionRuntime:
    store = ActionStore(conn)
    await store.initialize()
    registry = SkillRegistry([skill])
    return ActionRuntime(store, registry)


async def test_chart_analysis_agent_saves_observation_on_success(conn, memory):
    skill = FakeTradingSkill(
        outcome={
            "summary": "Bias is Bullish on EURUSD.",
            "data": {
                "instrument": "EURUSD",
                "speech_text": "Bias is Bullish on EURUSD. Clean breakout structure.",
            },
        }
    )
    runtime = await _make_runtime(conn, skill)
    agent = ChartAnalysisAgent(runtime, memory)

    result = await agent.run_once()

    assert result.status == AgentRunStatus.SUCCEEDED
    assert skill.calls == [{}]
    notes = await memory.list_notes(KIND_TRADING_OBSERVATION, symbol="EURUSD")
    assert len(notes) == 1
    assert "Bullish" in notes[0]["body"]
    assert notes[0]["actor"] == "agent:chart_analysis_agent"


async def test_chart_analysis_agent_falls_back_to_summary_without_speech_text(conn, memory):
    skill = FakeTradingSkill(outcome={"summary": "Chart analyzed."})
    runtime = await _make_runtime(conn, skill)
    agent = ChartAnalysisAgent(runtime, memory)

    result = await agent.run_once()

    assert result.status == AgentRunStatus.SUCCEEDED
    notes = await memory.list_notes(KIND_TRADING_OBSERVATION)
    assert notes[0]["body"] == "Chart analyzed."


async def test_chart_analysis_agent_fails_closed_without_fabricating_success(conn, memory):
    skill = FakeTradingSkill(
        outcome={
            "status": ActionStatus.FAILED,
            "summary": "Capture was refused or errored; nothing to analyze.",
            "error": "secure desktop capture refused",
        }
    )
    runtime = await _make_runtime(conn, skill)
    agent = ChartAnalysisAgent(runtime, memory)

    result = await agent.run_once()

    assert result.status == AgentRunStatus.FAILED
    assert result.error == "secure desktop capture refused"
    notes = await memory.list_notes(KIND_TRADING_OBSERVATION)
    assert notes == []
