"""MarketExplainerOrchestrator -- the one path for "what's happening with
<asset> today?" / "analyze gold" / "explain oil today" style requests
(mission: Universal Market Explainer).

Never hardcoded to one symbol. One call does:

    spoken asset -> AssetResolver -> exact symbol
    -> focus/open TradingView, set symbol (VERIFIED), set timeframe(s) (VERIFIED)
    -> fresh exact-window chart capture + vision read, per timeframe
    -> MarketContext (MT5 + calendar + news, already asset-agnostic)
    -> ONE deep-synthesis call combining everything collected
    -> one answer

An explicit on-demand request is the one case where mutating the user's
TradingView view is acceptable (mission section 2) -- this module is never
invoked for a fast-path quote/status question (those stay on
get_mt5_state/get_tradingview_state/get_economic_calendar/get_news, see
voice/gemini_live.py's system prompt).

Multi-timeframe collection is bounded (DEFAULT_TIMEFRAME_SEQUENCE, 3 entries)
and makes exactly ONE synthesis call at the end regardless of how many
timeframes were captured -- never one Claude call per timeframe.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from assistant.chart_analysis import ChartAnalysisResult
from assistant.chart_capture import capture_and_analyze_chart
from assistant.provider import AssistantProvider, AssistantRequest
from trading.asset_resolver import AssetResolver
from trading.market_context import MarketContext, build_market_context

Outcome = Literal["RESOLVED", "AMBIGUOUS", "NOT_FOUND", "CHART_UNAVAILABLE", "SYMBOL_NOT_VERIFIED"]

# Broader context -> intraday structure -> current session detail (mission
# section 3's own suggested sequence). Bounded to 3 so a "what's happening
# today" request never issues more than 3 chart captures + 3 vision reads
# before its one synthesis call.
DEFAULT_TIMEFRAME_SEQUENCE: tuple[str, ...] = ("4h", "1h", "15m")

_SYNTHESIS_SYSTEM_PROMPT = """You are TARS, synthesizing a Universal Market Explainer answer from evidence
TARS itself already gathered (MT5/TradingView/calendar/news/chart reads below) -- you are not looking at a
screenshot yourself here, only the structured text already extracted from one.

Never invent a price, event, headline, or chart detail not present in the evidence below. If MT5 is
disconnected or a timeframe's chart read failed, say so plainly rather than guessing.

Keep CATALYSTS/INTERPRETATION relevant to this asset's own class -- do not pad the answer with unrelated
developments just because they appeared in a generic news feed. FX: both currencies involved, their central
banks, rate differentials, relevant macro releases. Gold/silver: USD strength, real yields, inflation
expectations, risk sentiment, metal-specific supply/demand. Indices/equities: the index or company's own
news, sector context, earnings/corporate catalysts, broad macro and rates. Crypto: crypto-specific
regulatory/systemic/liquidity developments, not FX-style rate talk unless genuinely relevant. Oil: supply/
demand, inventories, OPEC+ decisions, geopolitical supply risk, USD strength where relevant. Never assume
causality just because something happened around the same time -- that is exactly what the causality
standard below is for.

Causality standard (never convert correlation into certainty):
- DIRECT: an explicit source/event with a tight temporal match to the move.
- STRONG TEMPORAL: a highly relevant event's timing overlaps the move.
- PLAUSIBLE: a relevant event exists but attribution is uncertain.
- INSUFFICIENT: no defensible catalyst found in the evidence -- say so.
Use language like "the timing is consistent with" or "may have contributed" -- never a bare causal claim
("X caused Y") unless the evidence genuinely supports that stronger statement.

Structure the answer with these sections where relevant (omit a section only if genuinely empty, never pad it):
CURRENT STATE (what is actually observed) / CONTEXT (higher-timeframe and intraday structure) /
CATALYSTS (calendar/news/events) / INTERPRETATION (plausible mechanisms, grounded in the evidence) /
SCENARIOS (upside condition, downside condition, range/uncertain case) /
INVALIDATION (what would weaken each interpretation) / UPCOMING RISK (scheduled catalysts ahead).
Never force a directional call -- "range/uncertain" is a legitimate scenario, not a failure to answer.
Reply for a voice assistant to read aloud: natural flowing sentences, no markdown headers/bullets/asterisks
read literally -- fold the structure above into prose, lead with the most useful fact."""


@dataclass
class ChartObservation:
    timeframe: str
    analysis: ChartAnalysisResult


@dataclass
class MarketExplainerResult:
    status: Outcome
    query: str
    symbol: str | None = None
    candidates: tuple[str, ...] = ()
    detail: str | None = None
    timeframes_analyzed: list[str] = field(default_factory=list)
    timeframes_failed: list[str] = field(default_factory=list)
    observations: list[ChartObservation] = field(default_factory=list)
    market_context: MarketContext | None = None
    answer: str = ""
    provider: str = ""

    @property
    def resolved(self) -> bool:
        return self.status == "RESOLVED"

    def evidence_text(self) -> str:
        """Everything collected, as one plain-text block for the single
        synthesis call -- never one call per timeframe/source."""
        parts: list[str] = []
        if self.market_context is not None:
            parts.append(self.market_context.as_evidence_text())
        for obs in self.observations:
            parts.append(f"[{obs.timeframe} chart read]\n{obs.analysis.formatted_tars_text()}")
        if self.timeframes_failed:
            parts.append(
                f"[Could not get a verified chart read for: {', '.join(self.timeframes_failed)} -- "
                "do not claim to have seen these.]"
            )
        return "\n\n".join(parts)


class MarketExplainerOrchestrator:
    def __init__(
        self,
        asset_resolver: AssetResolver,
        tradingview_adapter,
        chart_analysis_service,
        action_runtime,
        monitors,
        synthesis_provider: AssistantProvider,
        *,
        default_timeframes: tuple[str, ...] = DEFAULT_TIMEFRAME_SEQUENCE,
    ) -> None:
        self._resolver = asset_resolver
        self._tv = tradingview_adapter
        self._chart_analysis = chart_analysis_service
        self._actions = action_runtime
        self._monitors = monitors
        self._provider = synthesis_provider
        self._default_timeframes = default_timeframes

    async def analyze(
        self,
        asset: str,
        *,
        question: str = "",
        timeframe: str | None = None,
        active_symbol: str | None = None,
    ) -> MarketExplainerResult:
        resolved = self._resolver.resolve(asset, active_symbol=active_symbol)
        if resolved.outcome != "RESOLVED":
            return MarketExplainerResult(status=resolved.outcome, query=asset, candidates=resolved.candidates)
        symbol = resolved.symbol
        assert symbol is not None

        result = await self._bring_chart_into_view(symbol, asset)
        if result is not None:
            return result

        timeframes = [timeframe] if timeframe else list(self._default_timeframes)
        observations: list[ChartObservation] = []
        failed: list[str] = []
        for tf in timeframes:
            tf_outcome = await self._tv.set_timeframe(tf)
            if tf_outcome.get("status") != "SUCCEEDED":
                failed.append(tf)
                continue
            capture = await capture_and_analyze_chart(
                self._actions, self._chart_analysis,
                goal_text=f"Analyze this chart on the {tf} timeframe.",
                conversation_id=f"market-explainer:{symbol}:{tf}",
            )
            if not capture.analyzed:
                failed.append(tf)
                continue
            assert capture.result is not None
            observations.append(ChartObservation(timeframe=tf, analysis=capture.result))

        market_context = await build_market_context(self._monitors, self._tv, symbol)

        outcome = MarketExplainerResult(
            status="RESOLVED", query=asset, symbol=symbol,
            timeframes_analyzed=[o.timeframe for o in observations],
            timeframes_failed=failed,
            observations=observations,
            market_context=market_context,
        )

        synthesis_request = AssistantRequest(
            text=question.strip() or f"What is happening with {symbol} today?",
            conversation_id=f"market-explainer:{symbol}",
            system_context=_SYNTHESIS_SYSTEM_PROMPT + "\n\n[Evidence below]\n" + outcome.evidence_text(),
        )
        reply = await self._provider.respond(synthesis_request)
        outcome.answer = reply.text
        outcome.provider = reply.provider
        return outcome

    async def _bring_chart_into_view(self, symbol: str, asset: str) -> MarketExplainerResult | None:
        """Explicit market-analysis request: taking over the chart is
        acceptable (mission section 2), but every mutation is verified --
        never proceed on an unverified focus/symbol switch."""
        focus_outcome = await self._tv.focus()
        if focus_outcome.get("status") != "SUCCEEDED":
            return MarketExplainerResult(
                status="CHART_UNAVAILABLE", query=asset, symbol=symbol,
                detail=focus_outcome.get("summary") or "TradingView is not available.",
            )
        symbol_outcome = await self._tv.set_symbol(symbol)
        if symbol_outcome.get("status") != "SUCCEEDED":
            return MarketExplainerResult(
                status="SYMBOL_NOT_VERIFIED", query=asset, symbol=symbol,
                detail=symbol_outcome.get("summary") or f"Couldn't verify TradingView switched to {symbol}.",
            )
        return None
