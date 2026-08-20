"""Intelligence Router.

Directs user queries to the appropriate engine across 6 explicit conceptual domains:
1. MARKET_RESEARCH (macro drivers, market structure, economic context from real evidence)
2. CHART_ANALYSIS (qualitative multi-modal chart structure & key levels)
3. STRATEGY_EVALUATION (quant_brain state, active setups, entry validation)
4. TRADE_CALCULATION (deterministic risk/profit/capital mathematics)
5. GENERAL_CHAT (conversational companion intelligence, zero fake confidence)
6. DEVELOPER_REQUEST (developer workflows & tool operations)
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any

from assistant.provider import AssistantProvider, AssistantRequest
from intelligence.market_research import MarketResearchEngine
from intelligence.strategy_evaluation import StrategyEvaluationEngine
from intelligence.trade_calculation import TradeCalculationEngine


class IntentKind(str, Enum):
    MARKET_RESEARCH = "market_research"
    CHART_ANALYSIS = "chart_analysis"
    STRATEGY_EVALUATION = "strategy_evaluation"
    TRADE_CALCULATION = "trade_calculation"
    GENERAL_CHAT = "general_chat"
    DEVELOPER_REQUEST = "developer_request"


_CHART_ANALYSIS_PATTERN = re.compile(
    r"\b(analyze\s+(the\s+)?chart|look\s+at\s+(the\s+)?chart|read\s+(the\s+)?chart|"
    r"chart\s+analysis|check\s+(the\s+)?chart|what\s+do\s+you\s+see\s+on\s+(the\s+)?chart|"
    r"chart\s+screenshot|inspect\s+(the\s+)?chart)\b",
    re.IGNORECASE,
)

_STRATEGY_PATTERN = re.compile(
    r"\b(should\s+i\s+enter|can\s+i\s+enter|is\s+it\s+time\s+to\s+enter|is\s+there\s+a\s+setup|"
    r"active\s+setups?|what'?s\s+active|show\s+active|attention|why\s+invalidated|"
    r"last\s+invalidation|invalidation\s+reason)\b",
    re.IGNORECASE,
)

_DEVELOPER_PATTERN = re.compile(
    r"\b(git\s+status|git\s+diff|git\s+commit|git\s+log|worktree|pull\s+request|"
    r"pytest|test\s+suite|codebase|refactor|repo\s+structure|system_context|developer_tools)\b",
    re.IGNORECASE,
)

_CONFIDENCE_PATTERN = re.compile(
    r"\b(how\s+confident|what\s+is\s+your\s+confidence|confidence\s+(score|level|percentage))\b",
    re.IGNORECASE,
)

_CONFIDENCE_REPLY = (
    "### CONFIDENCE & STATISTICAL EDGE\n\n"
    "I do not invent or assign numerical confidence percentages (e.g., 85% or 95%). "
    "TARS is a quantitative companion and deterministic state router. "
    "Validated statistical edge, expected value, and trade validity originate exclusively from "
    "quant_brain models and ground-truth market rules."
)


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
        """Classifies incoming query intent across 6 distinct conceptual domains."""
        # 1. Trade Calculation Engine (deterministic risk/profit/capital mathematics)
        if TradeCalculationEngine.is_calculation_query(text):
            return IntentKind.TRADE_CALCULATION

        # 2. Chart Analysis Engine (explicit visual/screenshot domain)
        if _CHART_ANALYSIS_PATTERN.search(text):
            return IntentKind.CHART_ANALYSIS

        # 3. Strategy Evaluation Engine (quant_brain state & setups)
        if _STRATEGY_PATTERN.search(text) or StrategyEvaluationEngine.is_entry_inquiry(text):
            return IntentKind.STRATEGY_EVALUATION

        # 4. Market Research Engine (macro drivers, fundamentals, real evidence)
        if MarketResearchEngine.is_research_query(text):
            return IntentKind.MARKET_RESEARCH

        # 5. Developer Request
        if _DEVELOPER_PATTERN.search(text):
            return IntentKind.DEVELOPER_REQUEST

        # 6. General Chat (conversational companion intelligence)
        return IntentKind.GENERAL_CHAT

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

        if intent == IntentKind.CHART_ANALYSIS:
            msg = (
                "### CHART ANALYSIS\n\n"
                "Chart analysis requires a visual chart capture. "
                "Use the native Chart Capture trigger (or HUD Analyze Chart) to capture "
                "the active TradingView window and generate structured market structure analysis."
            )
            return intent, msg, "deterministic"

        if intent == IntentKind.STRATEGY_EVALUATION:
            active = []
            if self._events is not None:
                active = await self._events.get_active_setups()
            if StrategyEvaluationEngine.is_entry_inquiry(text):
                reply_text = StrategyEvaluationEngine.evaluate_entry_decision(active)
            else:
                rep = StrategyEvaluationEngine.evaluate_setups(active)
                reply_text = rep.formatted_text
            return intent, reply_text, "deterministic"

        if intent == IntentKind.MARKET_RESEARCH:
            report = await self._research_engine.generate_research(
                text, conversation_id, active_context=active_context
            )
            return intent, report.content, report.provider

        if intent == IntentKind.GENERAL_CHAT and _CONFIDENCE_PATTERN.search(text):
            return intent, _CONFIDENCE_REPLY, "deterministic"

        # General assistant fallthrough is handled by caller/router
        return IntentKind.GENERAL_CHAT, "", self._provider.name

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

        if intent == IntentKind.CHART_ANALYSIS:
            msg = (
                "### CHART ANALYSIS\n\n"
                "Chart analysis requires a visual chart capture. "
                "Use the native Chart Capture trigger (or HUD Analyze Chart) to capture "
                "the active TradingView window and generate structured market structure analysis."
            )
            yield {"type": "delta", "text": msg}
            yield {
                "type": "complete",
                "text": msg,
                "provider": "deterministic",
                "intent": intent.value,
            }
            return

        if intent == IntentKind.STRATEGY_EVALUATION:
            active = []
            if self._events is not None:
                active = await self._events.get_active_setups()
            if StrategyEvaluationEngine.is_entry_inquiry(text):
                reply_text = StrategyEvaluationEngine.evaluate_entry_decision(active)
            else:
                rep = StrategyEvaluationEngine.evaluate_setups(active)
                reply_text = rep.formatted_text
            yield {"type": "delta", "text": reply_text}
            yield {
                "type": "complete",
                "text": reply_text,
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

        if intent == IntentKind.GENERAL_CHAT and _CONFIDENCE_PATTERN.search(text):
            yield {"type": "delta", "text": _CONFIDENCE_REPLY}
            yield {
                "type": "complete",
                "text": _CONFIDENCE_REPLY,
                "provider": "deterministic",
                "intent": intent.value,
            }
            return
