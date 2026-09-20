from __future__ import annotations

import aiosqlite
import pytest

from actions.registry import SkillRegistry
from actions.runtime import ActionRuntime
from actions.store import ActionStore
from agents.models import AgentRunStatus
from agents.trading_workspace_agent import TradingWorkspaceAgent
from app.action_contracts import (
    ActionRequest,
    ActionResult,
    ActionStatus,
    BaseSkill,
    RiskLevel,
)
from storage.migrator import run_migrations


class FakeTradingSkill(BaseSkill):
    """Stand-in for the real `trading` skill's focus/open TradingView
    actions, controllable per test -- see test_agent_chart_analysis.py's
    version of the same pattern."""

    name = "trading"
    capabilities: tuple[str, ...] = ("focus_tradingview", "open_tradingview")

    def __init__(self, *, outcomes: dict[str, dict] | None = None) -> None:
        self._outcomes = outcomes or {}
        self.calls: list[str] = []

    def classify_risk(self, action: str, arguments: dict) -> RiskLevel:
        return RiskLevel.LOW_RISK

    async def validate(self, action: str, arguments: dict) -> None:
        return None

    async def execute(self, request: ActionRequest) -> ActionResult:
        self.calls.append(request.action)
        outcome = self._outcomes.get(request.action, {})
        return self._result(
            request,
            outcome.get("status", ActionStatus.SUCCEEDED),
            outcome.get("summary", f"{request.action} ok"),
            risk_level=RiskLevel.LOW_RISK,
            data=outcome.get("data") or {},
            error=outcome.get("error"),
        )


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "trading_workspace_agent_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


async def _make_runtime(conn, skill: FakeTradingSkill) -> ActionRuntime:
    store = ActionStore(conn)
    await store.initialize()
    registry = SkillRegistry([skill])
    return ActionRuntime(store, registry)


async def test_focuses_existing_window_without_opening_a_browser(conn):
    skill = FakeTradingSkill(outcomes={"focus_tradingview": {"summary": "Focused 'TradingView - Chart'."}})
    runtime = await _make_runtime(conn, skill)
    agent = TradingWorkspaceAgent(runtime)

    result = await agent.run_once()

    assert result.status == AgentRunStatus.SUCCEEDED
    assert skill.calls == ["focus_tradingview"]
    assert "Focused" in result.summary


async def test_falls_back_to_open_when_focus_fails(conn):
    skill = FakeTradingSkill(
        outcomes={
            "focus_tradingview": {
                "status": ActionStatus.FAILED,
                "summary": "No running TradingView window was found to focus.",
                "error": "no visible window matched 'tradingview'",
            },
            "open_tradingview": {"summary": "Opened TradingView in the default browser."},
        }
    )
    runtime = await _make_runtime(conn, skill)
    agent = TradingWorkspaceAgent(runtime)

    result = await agent.run_once()

    assert result.status == AgentRunStatus.SUCCEEDED
    assert skill.calls == ["focus_tradingview", "open_tradingview"]
    assert "Opened" in result.summary


async def test_fails_honestly_when_both_focus_and_open_fail(conn):
    skill = FakeTradingSkill(
        outcomes={
            "focus_tradingview": {
                "status": ActionStatus.FAILED,
                "summary": "No running TradingView window was found to focus.",
                "error": "no visible window matched 'tradingview'",
            },
            "open_tradingview": {
                "status": ActionStatus.FAILED,
                "summary": "Failed to open TradingView.",
                "error": "no browser available",
            },
        }
    )
    runtime = await _make_runtime(conn, skill)
    agent = TradingWorkspaceAgent(runtime)

    result = await agent.run_once()

    assert result.status == AgentRunStatus.FAILED
    assert result.error == "no browser available"
    assert skill.calls == ["focus_tradingview", "open_tradingview"]
