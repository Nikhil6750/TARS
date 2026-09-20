"""ChartAnalysisAgent — an ON_DEMAND agent wrapping the same
`trading.analyze_active_chart` action a manual "analyze this chart" request
already runs, then persists the qualitative read as a searchable trading
observation. This is a convenience sequencer over the existing
ActionRuntime/TradingSkill, not a second chart-analysis implementation --
see `assistant/chart_analysis.py`'s `ChartAnalysisResult`, which already
carries the "not a quant_brain-validated signal" disclaimer this agent
simply stores verbatim.
"""
from __future__ import annotations

from actions.runtime import ActionRuntime
from agents.base import Agent
from agents.models import AgentConfig, AgentMode, AgentRunResult, AgentRunStatus
from app.action_contracts import ActionRequest, ActionSource, ActionStatus
from memory.service import MemoryService

# Chart analysis calls out to an LLM (ChartAnalysisService -> AssistantProvider),
# so it needs materially more headroom than the framework's 60s default.
_DEFAULT_CONFIG = AgentConfig(mode=AgentMode.ON_DEMAND, timeout_seconds=90.0)


class ChartAnalysisAgent(Agent):
    name = "chart_analysis_agent"

    def __init__(
        self,
        action_runtime: ActionRuntime,
        memory_service: MemoryService,
        *,
        config: AgentConfig | None = None,
    ) -> None:
        super().__init__(config or _DEFAULT_CONFIG)
        self._action_runtime = action_runtime
        self._memory = memory_service

    async def run_once(self) -> AgentRunResult:
        result = await self._action_runtime.submit(
            ActionRequest(
                skill="trading",
                action="analyze_active_chart",
                arguments={},
                source=ActionSource.deterministic,
            )
        )
        if result.status != ActionStatus.SUCCEEDED:
            # Fail closed -- never invent an observation for a capture/
            # analysis that didn't actually happen.
            return AgentRunResult(
                status=AgentRunStatus.FAILED,
                summary="Chart analysis action did not succeed.",
                error=result.error or result.summary,
                data=result.data,
            )

        observation_text = result.data.get("speech_text") or result.summary
        await self._memory.save_trading_observation(
            observation_text,
            symbol=result.data.get("instrument"),
            actor="agent:chart_analysis_agent",
            tags=["chart_analysis_agent"],
        )
        return AgentRunResult(
            status=AgentRunStatus.SUCCEEDED,
            summary=result.summary,
            data=result.data,
        )
