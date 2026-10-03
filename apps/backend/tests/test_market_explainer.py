"""MarketExplainerOrchestrator -- the Universal Market Explainer's one
orchestration path. Fakes TradingViewAdapter/monitors/action_runtime at the
same seams their own test files already define (test_tradingview_adapter.py,
test_market_context.py, test_chart_analysis_turn.py); uses the REAL
AssetResolver since that is already independently tested and cheap to run
unmocked here.
"""
from __future__ import annotations

import base64
import io

from PIL import Image

from app.action_contracts import ActionResult, ActionStatus, RiskLevel
from assistant.chart_analysis import ChartAnalysisService
from assistant.provider import AssistantProvider, AssistantReply, AssistantRequest
from trading.asset_resolver import AssetResolver
from trading.market_explainer import MarketExplainerOrchestrator

_STRUCTURED_JSON = """{
  "instrument": "%s", "timeframe": "%s", "current_price_context": "near range mid",
  "supply_zone": "resistance", "demand_zone": "support", "recent_price_sequence": "drifting",
  "at_meaningful_location": false, "market_context": "Quiet session.", "key_levels": [],
  "possible_setup": null, "invalidation": null, "risk_notes": "none"
}"""


class _FakeChartProvider(AssistantProvider):
    name = "fake_chart"

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        # Echo back whatever timeframe the goal text mentions, so each
        # collected observation is distinguishable in assertions.
        tf = "unknown"
        for candidate in ("4h", "1h", "15m", "5m"):
            if candidate in request.text:
                tf = candidate
                break
        return AssistantReply(text=_STRUCTURED_JSON % ("XAUUSD", tf), provider=self.name)


class _FakeSynthesisProvider(AssistantProvider):
    name = "fake_synthesis"

    def __init__(self):
        self.calls: list[AssistantRequest] = []

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        self.calls.append(request)
        return AssistantReply(text="Gold is quiet, range-bound.", provider=self.name)


class _FakeActionRuntime:
    def __init__(self, image_b64: str | None = None):
        self.requests = []
        self._image_b64 = image_b64 or _bmp_data_uri()

    async def submit(self, request):
        self.requests.append(request)
        return ActionResult(
            request_id=request.id, status=ActionStatus.SUCCEEDED, risk_level=RiskLevel.READ_ONLY,
            summary="captured",
            data={"executable": "TradingView.exe", "window_title": "XAUUSD - TradingView",
                 "image_data_base64": self._image_b64, "image_format": "image/bmp",
                 "is_secure_desktop": False, "error": None},
        )


def _bmp_data_uri() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color=(10, 20, 30)).save(buf, format="BMP")
    return "data:image/bmp;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


class _FakeTVAdapter:
    def __init__(self, *, focus_ok=True, set_symbol_ok=True, set_timeframe_ok=True, monitor_result=None):
        self.focus_calls = 0
        self.set_symbol_calls: list[str] = []
        self.set_timeframe_calls: list[str] = []
        self._focus_ok = focus_ok
        self._set_symbol_ok = set_symbol_ok
        self._set_timeframe_ok = set_timeframe_ok
        self._monitor_result = monitor_result or {"monitoring": False, "symbol": None, "timeframe": None}

    async def focus(self):
        self.focus_calls += 1
        return {"status": "SUCCEEDED"} if self._focus_ok else {"status": "FAILED", "summary": "TradingView is not running."}

    async def set_symbol(self, symbol):
        self.set_symbol_calls.append(symbol)
        ok = self._set_timeframe_decider(self._set_symbol_ok, symbol)
        return {"status": "SUCCEEDED"} if ok else {"status": "FAILED", "summary": f"Couldn't verify {symbol}."}

    async def set_timeframe(self, timeframe):
        self.set_timeframe_calls.append(timeframe)
        ok = self._set_timeframe_decider(self._set_timeframe_ok, timeframe)
        return {"status": "SUCCEEDED"} if ok else {"status": "FAILED", "summary": f"Couldn't verify {timeframe}."}

    async def monitor_chart(self):
        return self._monitor_result

    @staticmethod
    def _set_timeframe_decider(spec, value):
        return spec(value) if callable(spec) else spec


class _FakeMonitors:
    news = None

    async def status(self):
        return {"mt5": {"state": "DISCONNECTED", "quotes": {}, "positions": [], "floating_pnl": None},
                "calendar": {"next": None}}


def _orchestrator(**kwargs) -> tuple[MarketExplainerOrchestrator, _FakeTVAdapter, _FakeActionRuntime, _FakeSynthesisProvider]:
    tv = kwargs.pop("tv", None) or _FakeTVAdapter()
    runtime = kwargs.pop("runtime", None) or _FakeActionRuntime()
    synthesis = kwargs.pop("synthesis", None) or _FakeSynthesisProvider()
    orchestrator = MarketExplainerOrchestrator(
        AssetResolver(None), tv, ChartAnalysisService(_FakeChartProvider()), runtime, _FakeMonitors(), synthesis,
        **kwargs,
    )
    return orchestrator, tv, runtime, synthesis


async def test_resolves_asset_switches_tradingview_and_makes_one_synthesis_call():
    orchestrator, tv, _runtime, synthesis = _orchestrator()
    result = await orchestrator.analyze("gold", timeframe="15m")

    assert result.resolved
    assert result.symbol == "XAUUSD"
    assert tv.focus_calls == 1
    assert tv.set_symbol_calls == ["XAUUSD"]
    assert tv.set_timeframe_calls == ["15m"]
    assert len(synthesis.calls) == 1  # exactly one deep-synthesis call
    assert result.answer == "Gold is quiet, range-bound."
    assert "XAUUSD" in result.evidence_text()


async def test_ambiguous_asset_never_mutates_tradingview():
    orchestrator, tv, _runtime, synthesis = _orchestrator()
    result = await orchestrator.analyze("nasdaq")

    assert result.status == "AMBIGUOUS"
    assert set(result.candidates) == {"US100", "NAS100", "USTEC", "NDX"}
    assert tv.focus_calls == 0 and tv.set_symbol_calls == []
    assert synthesis.calls == []


async def test_not_found_asset_never_mutates_tradingview():
    orchestrator, tv, _runtime, synthesis = _orchestrator()
    result = await orchestrator.analyze("purple elephant currency")

    assert result.status == "NOT_FOUND"
    assert tv.focus_calls == 0
    assert synthesis.calls == []


async def test_unverified_symbol_switch_stops_before_any_chart_capture():
    tv = _FakeTVAdapter(set_symbol_ok=False)
    orchestrator, tv, runtime, synthesis = _orchestrator(tv=tv)
    result = await orchestrator.analyze("gold", timeframe="15m")

    assert result.status == "SYMBOL_NOT_VERIFIED"
    assert result.symbol == "XAUUSD"
    assert runtime.requests == []  # no capture was ever attempted
    assert synthesis.calls == []


async def test_tradingview_unavailable_stops_before_set_symbol():
    tv = _FakeTVAdapter(focus_ok=False)
    orchestrator, tv, runtime, synthesis = _orchestrator(tv=tv)
    result = await orchestrator.analyze("gold")

    assert result.status == "CHART_UNAVAILABLE"
    assert tv.set_symbol_calls == []
    assert runtime.requests == []


async def test_default_multi_timeframe_sequence_collects_observations_with_one_synthesis_call():
    orchestrator, tv, _runtime, synthesis = _orchestrator()
    result = await orchestrator.analyze("gold")  # no explicit timeframe -> default sequence

    assert tv.set_timeframe_calls == ["1h", "15m"]
    assert result.timeframes_analyzed == ["1h", "15m"]
    assert len(result.observations) == 2
    assert len(synthesis.calls) == 1  # still exactly one synthesis call for all of them


async def test_a_failed_timeframe_is_noted_honestly_not_fabricated():
    tv = _FakeTVAdapter(set_timeframe_ok=lambda tf: tf != "1h")
    orchestrator, tv, _runtime, synthesis = _orchestrator(tv=tv)
    result = await orchestrator.analyze("gold")

    assert result.timeframes_analyzed == ["15m"]
    assert result.timeframes_failed == ["1h"]
    assert "Could not get a verified chart read for: 1h" in result.evidence_text()
    assert "do not claim to have seen these" in result.evidence_text()


async def test_mt5_disconnected_still_produces_an_answer():
    orchestrator, _tv, _runtime, synthesis = _orchestrator()
    result = await orchestrator.analyze("gold", timeframe="15m")

    assert result.resolved
    assert len(synthesis.calls) == 1
    assert "DISCONNECTED" in result.evidence_text()


async def test_follow_up_pronoun_resolves_via_active_symbol():
    orchestrator, tv, _runtime, synthesis = _orchestrator()
    result = await orchestrator.analyze("it", timeframe="5m", active_symbol="EURUSD")

    assert result.resolved
    assert result.symbol == "EURUSD"
    assert tv.set_symbol_calls == ["EURUSD"]
