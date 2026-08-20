"""TARS Trading Intelligence Architecture Package.

Provides four distinct analytical engines:
1. Trade Calculation Engine (TradeCalculationEngine)
2. Market Research Engine (MarketResearchEngine)
3. Strategy Evaluation Engine (StrategyEvaluationEngine)
4. Chart Analysis Engine & Composer (IntelligenceComposer)
"""
from __future__ import annotations

from intelligence.composer import IntelligenceComposer
from intelligence.market_research import MarketResearchEngine, MarketResearchReport
from intelligence.router import IntelligenceRouter, IntentKind
from intelligence.strategy_evaluation import StrategyEvaluationEngine, StrategyEvaluationReport
from intelligence.trade_calculation import (
    TradeCalculationEngine,
    TradeCalculationParams,
    TradeCalculationResult,
)

__all__ = [
    "IntelligenceComposer",
    "IntelligenceRouter",
    "IntentKind",
    "MarketResearchEngine",
    "MarketResearchReport",
    "StrategyEvaluationEngine",
    "StrategyEvaluationReport",
    "TradeCalculationEngine",
    "TradeCalculationParams",
    "TradeCalculationResult",
]
