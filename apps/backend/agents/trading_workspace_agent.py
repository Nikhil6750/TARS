"""TradingWorkspaceAgent — an ON_DEMAND agent for "get my trading workspace
ready": tries to focus an already-open TradingView window first, and only
falls back to opening a fresh TradingView tab if no such window exists (or
focusing it fails). Both paths dispatch through the exact same `trading`
skill actions a manual HUD/voice command would use -- this agent is a
sequencer, not a second window-management implementation.
"""
from __future__ import annotations

from actions.runtime import ActionRuntime
from agents.base import Agent
from agents.models import AgentConfig, AgentMode, AgentRunResult, AgentRunStatus
from app.action_contracts import ActionRequest, ActionSource, ActionStatus

_DEFAULT_CONFIG = AgentConfig(mode=AgentMode.ON_DEMAND, timeout_seconds=30.0)


class TradingWorkspaceAgent(Agent):
    name = "trading_workspace_agent"

    def __init__(self, action_runtime: ActionRuntime, *, config: AgentConfig | None = None) -> None:
        super().__init__(config or _DEFAULT_CONFIG)
        self._action_runtime = action_runtime

    async def run_once(self) -> AgentRunResult:
        focus_result = await self._action_runtime.submit(
            ActionRequest(
                skill="trading",
                action="focus_tradingview",
                arguments={},
                source=ActionSource.deterministic,
            )
        )
        if focus_result.status == ActionStatus.SUCCEEDED:
            return AgentRunResult(
                status=AgentRunStatus.SUCCEEDED,
                summary=f"Focused an existing TradingView window: {focus_result.summary}",
                data=focus_result.data,
            )

        open_result = await self._action_runtime.submit(
            ActionRequest(
                skill="trading",
                action="open_tradingview",
                arguments={},
                source=ActionSource.deterministic,
            )
        )
        if open_result.status == ActionStatus.SUCCEEDED:
            return AgentRunResult(
                status=AgentRunStatus.SUCCEEDED,
                summary=f"No focusable TradingView window; opened one instead: {open_result.summary}",
                data=open_result.data,
            )

        # Both paths genuinely failed -- report the real error, never a
        # fabricated "workspace ready".
        return AgentRunResult(
            status=AgentRunStatus.FAILED,
            summary="Could not focus or open TradingView.",
            error=open_result.error or open_result.summary,
            data={"focus_error": focus_result.error, "open_error": open_result.error},
        )
