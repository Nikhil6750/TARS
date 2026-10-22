"""Deterministic intent + risk router. No model is consulted: simple actions run locally, chat goes to Gemini Live, and only trading or
research requests reach Claude/ChatGPT. Reuses the existing offline LocalIntentRouter grammar for simple actions."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import Enum

from voice.local_intents import LocalIntentRouter


class Route(str, Enum):
    SIMPLE_ACTION = "simple_action"
    CHAT = "chat"
    TRADING_QUESTION = "trading_question"
    RESEARCH_REQUEST = "research_request"


_RESEARCH = re.compile(r"\b(H\d{4}|experiment|backtest|back-test|hypothesis|walk[- ]?forward|validation|out[- ]of[- ]sample|expectancy|"
                       r"research (?:result|finding|memory)|did .{0,30}(?:survive|pass|fail)|strategy (?:test|result)|quant agent)\b", re.I)
_TRADING = re.compile(r"\b(xauusd|eurusd|gbpusd|usdjpy|btcusd|gold|silver|forex|breakout|bullish|bearish|support|resistance|trend|momentum|"
                      r"setup|entry|should i (?:buy|sell|enter)|long|short|volatility|spread|market|chart|price action|london open|asian range|"
                      r"what('?s| is) (?:gold|the market)|analy[sz]e)\b", re.I)


@dataclass(frozen=True)
class RouteDecision:
    route: Route
    models: tuple[str, ...]
    needs_independent_review: bool
    reason: str
    local_intent: object | None = None
    router_seconds: float = 0.0


class IntentRiskRouter:
    def __init__(self, local: LocalIntentRouter | None = None) -> None:
        self.local = local or LocalIntentRouter()

    def route(self, text: str) -> RouteDecision:
        t0 = time.perf_counter()
        text = (text or "").strip()
        li = self.local.route(text)
        if li is not None and not li.needs_cloud:      # needs_cloud intents (e.g. market_analysis) are questions, not local actions
            return self._done(Route.SIMPLE_ACTION, (), False, "matched a deterministic local command", t0, li)
        if _RESEARCH.search(text):
            return self._done(Route.RESEARCH_REQUEST, ("claude", "quant", "chatgpt"), True, "research/experiment request", t0)
        if _TRADING.search(text):
            return self._done(Route.TRADING_QUESTION, ("claude", "chatgpt"), True, "trading question needs evidence + independent review", t0)
        return self._done(Route.CHAT, ("gemini",), False, "ordinary conversation", t0)

    @staticmethod
    def _done(route, models, review, reason, t0, li=None) -> RouteDecision:
        return RouteDecision(route, models, review, reason, li, time.perf_counter() - t0)
