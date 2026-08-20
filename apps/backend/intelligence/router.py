"""Intelligence Router.

Directs user queries to the appropriate engine:
1. Trade Calculation Engine (deterministic risk/profit/capital mathematics)
2. Market Research Engine (macro drivers, market structure, economic context)
3. Strategy Evaluation Engine (quant_brain state & active setups)
4. General Assistant (conversational companion intelligence)
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from assistant.provider import AssistantProvider
from intelligence.market_research import MarketResearchEngine
from intelligence.strategy_evaluation import StrategyEvaluationEngine
from intelligence.trade_calculation import TradeCalculationEngine


class IntentKind(str, Enum):
    TRADE_CALCULATION = "trade_calculation"
    MARKET_RESEARCH = "market_research"
    STRATEGY_EVALUATION = "strategy_evaluation"
    GENERAL_ASSISTANT = "general_assistant"


class IntelligenceRouter:
    """Routes queries to dedicated intelligence engines."""

    def __init__(
        self,
        provider: AssistantProvider,
        memory_service: Any | None = None,
        event_service: Any | None = None,
    ) -> None:
        self._provider = provider
        self._memory = memory_service
        self._events = event_service
        self._research_engine = MarketResearchEngine(provider, memory_service)

    def classify_intent(self, text: str) -> IntentKind:
        """Classifies incoming query intent."""
        if TradeCalculationEngine.is_calculation_query(text):
            return IntentKind.TRADE_CALCULATION
        if MarketResearchEngine.is_research_query(text):
            return IntentKind.MARKET_RESEARCH
        return IntentKind.GENERAL_ASSISTANT

    async def handle(
        self,
        text: str,
        conversation_id: str,
        active_context: str | None = None,
    ) -> tuple[IntentKind, str, str]:
        """Handles synchronous query execution.

        Returns (intent, reply_text, provider_name).
        """
        intent = self.classify_intent(text)

        if intent == IntentKind.TRADE_CALCULATION:
            res = TradeCalculationEngine.evaluate(text)
            return intent, res.formatted_text, "deterministic"

        if intent == IntentKind.MARKET_RESEARCH:
            report = await self._research_engine.generate_research(
                text, conversation_id, active_context=active_context
            )
            return intent, report.content, report.provider

        # General assistant fallthrough is handled by caller/router
        return IntentKind.GENERAL_ASSISTANT, "", self._provider.name

    async def handle_stream(
        self,
        text: str,
        conversation_id: str,
        active_context: str | None = None,
    ):
        """Streams reply events as delta / complete."""
        intent = self.classify_intent(text)

        if intent == IntentKind.TRADE_CALCULATION:
            res = TradeCalculationEngine.evaluate(text)
            yield {"type": "delta", "text": res.formatted_text}
            yield {
                "type": "complete",
                "text": res.formatted_text,
                "provider": "deterministic",
                "intent": intent.value,
            }
            return

        if intent == IntentKind.MARKET_RESEARCH:
            accumulated = ""
            async for event in self._research_engine.generate_research_stream(
                text, conversation_id, active_context=active_context
            ):
                if event.get("type") == "delta":
                    chunk = event.get("text", "")
                    accumulated += chunk
                    yield event
                elif event.get("type") == "complete":
                    final_text = event.get("text") or accumulated
                    yield {
                        "type": "complete",
                        "text": final_text,
                        "provider": event.get("provider", self._provider.name),
                        "intent": intent.value,
                    }
                    return
            if accumulated:
                yield {
                    "type": "complete",
                    "text": accumulated,
                    "provider": self._provider.name,
                    "intent": intent.value,
                }
            return
