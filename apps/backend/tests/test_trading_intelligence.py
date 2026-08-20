"""Tests for TARS Professional Trading Intelligence Architecture."""
import pytest

from assistant.chart_analysis import ChartAnalysisResult, _split_capital_question
from assistant.provider import AssistantProvider, AssistantReply, AssistantRequest
from intelligence.composer import IntelligenceComposer
from intelligence.market_research import MarketResearchEngine
from intelligence.router import IntelligenceRouter, IntentKind
from intelligence.strategy_evaluation import StrategyEvaluationEngine
from intelligence.trade_calculation import TradeCalculationEngine


class MockAssistantProvider(AssistantProvider):
    @property
    def name(self) -> str:
        return "mock_intelligence_provider"

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        return AssistantReply(
            text=(
                "### MACRO DRIVERS\n"
                "• Federal Reserve maintaining restrictive monetary posture with focus on PCE data.\n"
                "• Treasury yields stabilizing around 4.25%.\n\n"
                "### MARKET STRUCTURE & REGIME\n"
                "• Gold remains in multi-week bullish consolidation.\n"
                "• Order flow reflects institutional accumulation at demand levels.\n\n"
                "### KEY CATALYSTS & EVENTS\n"
                "• Upcoming FOMC minutes and Jackson Hole symposium.\n\n"
                "### CROSS-ASSET CONTEXT\n"
                "• DXY showing mild pullback, supporting commodity upside.\n\n"
                "### RISK FACTORS\n"
                "• Elevated CPI print risk could trigger rate hike expectations."
            ),
            provider=self.name,
        )


def test_trade_calculation_missing_parameters():
    query = "estimate the profit if my capital was 10000 rupees"
    assert TradeCalculationEngine.is_calculation_query(query)
    
    res = TradeCalculationEngine.evaluate(query)
    assert not res.has_sufficient_params
    assert "### CAPITAL / PROFIT ESTIMATE" in res.formatted_text
    assert "entry" in res.formatted_text.lower()
    assert "stop loss" in res.formatted_text.lower()
    assert "target" in res.formatted_text.lower()
    assert "not going to invent a rupee figure" in res.formatted_text


def test_trade_calculation_with_complete_parameters():
    query = "capital is ₹10,000, risk is 2%, entry is 2700, SL is 2690, TP is 2730"
    assert TradeCalculationEngine.is_calculation_query(query)
    
    res = TradeCalculationEngine.evaluate(query)
    assert res.has_sufficient_params
    assert "### CAPITAL / PROFIT CALCULATION" in res.formatted_text
    assert res.risk_amount == 200.0  # 2% of 10,000
    assert res.risk_reward_ratio == 3.0  # (2730 - 2700) / (2700 - 2690) = 30 / 10 = 3.0
    assert res.target_profit == 600.0  # 200 * 3 = 600
    assert "Projected Profit at Target: ₹600.00" in res.formatted_text
    assert "Risk:Reward Ratio: 1:3.00" in res.formatted_text


@pytest.mark.asyncio
async def test_market_research_engine():
    provider = MockAssistantProvider()
    engine = MarketResearchEngine(provider=provider)
    
    assert engine.is_research_query("Research the current market structure and key macro drivers")
    
    report = await engine.generate_research(
        query="Research the current market structure and key macro drivers",
        conversation_id="test-conv",
    )
    assert "### MACRO DRIVERS" in report.content
    assert "### MARKET STRUCTURE & REGIME" in report.content
    assert "### KEY CATALYSTS & EVENTS" in report.content
    assert "### CROSS-ASSET CONTEXT" in report.content
    assert "### RISK FACTORS" in report.content
    assert "CURRENT STATE" not in report.content


def test_strategy_evaluation_engine_not_configured():
    report = StrategyEvaluationEngine.evaluate_setups(
        active_setups=[],
        strategy_status="NOT_CONFIGURED",
    )
    assert not report.is_strategy_configured
    assert "### STRATEGY / QUANT VALIDATION" in report.formatted_text
    assert "Strategy Engine: `NOT_CONFIGURED`" in report.formatted_text
    assert "quant_brain" in report.formatted_text


def test_strategy_evaluation_engine_active_setups():
    setups = [
        {
            "symbol": "XAUUSD",
            "direction": "LONG",
            "entry": 2680.50,
            "stop_loss": 2672.00,
            "target": 2705.00,
            "risk_reward": 2.88,
            "state": "SETUP_VALID",
        }
    ]
    report = StrategyEvaluationEngine.evaluate_setups(
        active_setups=setups,
        strategy_status="CONFIGURED",
    )
    assert report.is_strategy_configured
    assert "XAUUSD (LONG)" in report.formatted_text
    assert "Entry: `2680.5`" in report.formatted_text


def test_intelligence_composer_chart_formatting():
    formatted = IntelligenceComposer.format_chart_response(
        instrument="XAUUSD",
        timeframe="15M",
        current_price_context="2684.50, testing supply zone",
        supply_zone="2690.00 - 2695.00",
        demand_zone="2660.00 - 2665.00",
        recent_price_sequence="Bullish impulse followed by range compression",
        bias="Bullish",
        what_i_see="High volume breakout retesting previous resistance as demand.",
        setup="Breakout & Retest",
        key_levels=["Resistance: 2700.00", "Support: 2660.00", "Demand: 2645.00"],
        invalidation="Close below 2660.00 on 15M",
        risk_notes="Upcoming US session open may increase volatility.",
        action="Watch for Confirmation",
    )
    
    assert "### INSTRUMENT\nXAUUSD · 15M" in formatted
    assert "### STRUCTURE" in formatted
    assert "- Current price: 2684.50, testing supply zone" in formatted
    assert "- Supply zone: 2690.00 - 2695.00" in formatted
    assert "BIAS: Bullish" in formatted
    assert "### WHAT I SEE" in formatted
    assert "### SETUP\nBreakout & Retest" in formatted
    assert "### KEY LEVELS\n- Resistance: 2700.00\n- Support: 2660.00\n- Demand: 2645.00" in formatted
    assert "### INVALIDATION\nClose below 2660.00 on 15M" in formatted
    assert "### RISK\nUpcoming US session open" in formatted
    assert "ACTION: Watch for Confirmation" in formatted


def test_chart_analysis_split_capital_question():
    text = "analyze the chart and estimate the profit if my capital was 10000 rupees"
    goal, capital_note = _split_capital_question(text)
    assert goal == "Analyze this chart."
    assert capital_note is not None
    assert "### CAPITAL / PROFIT ESTIMATE" in capital_note
    assert "entry" in capital_note.lower()
