from __future__ import annotations

import io

import aiosqlite
import pytest
from PIL import Image

from assistant.chart_analysis import ChartAnalysisResult, ChartAnalysisService
from assistant.deep_verification import materially_different, run_deep_verification
from assistant.hot_chart_state_store import HotChartStateStore
from assistant.provider import AssistantProvider, AssistantReply, AssistantRequest
from storage.migrator import run_migrations


def _result(**overrides) -> ChartAnalysisResult:
    base = dict(
        instrument="XAUUSD",
        timeframe="15M",
        market_context="Ranging near resistance.",
        key_levels=["Resistance: 4550", "Support: 4500"],
        possible_setup=None,
        invalidation="Break below 4500",
        risk_notes="Macro/event risk has not been checked in this chart-only analysis.",
        provider="claude_code",
        raw_text="{}",
        structured=True,
        bias="Neutral",
        setup="Unclear",
        action="Watch",
        supply_zone="4550",
        demand_zone="4500",
    )
    base.update(overrides)
    return ChartAnalysisResult(**base)


def test_identical_results_are_not_materially_different():
    a = _result()
    b = _result()
    assert materially_different(a, b) is False


def test_different_bias_is_material():
    assert materially_different(_result(bias="Bullish"), _result(bias="Bearish")) is True


def test_different_setup_is_material():
    assert materially_different(_result(setup="Valid"), _result(setup="Invalid")) is True


def test_different_action_is_material():
    assert materially_different(_result(action="Watch"), _result(action="Potential setup")) is True


def test_different_invalidation_is_material():
    assert materially_different(
        _result(invalidation="Break below 4500"), _result(invalidation="Break below 4400")
    ) is True


def test_different_key_levels_is_material():
    assert materially_different(
        _result(key_levels=["Resistance: 4550"]), _result(key_levels=["Resistance: 4600"])
    ) is True


def test_key_level_order_alone_is_not_material():
    assert materially_different(
        _result(key_levels=["A", "B"]), _result(key_levels=["B", "A"])
    ) is False


def test_wording_only_change_in_market_context_is_not_material():
    a = _result(market_context="Price is consolidating near resistance.")
    b = _result(market_context="Price is currently ranging just below resistance.")
    assert materially_different(a, b) is False


def test_unstructured_results_compared_by_raw_text():
    a = _result(structured=False, raw_text="I see a candlestick chart.")
    b = _result(structured=False, raw_text="I see a candlestick chart.")
    assert materially_different(a, b) is False

    c = _result(structured=False, raw_text="I see a candlestick chart with a breakout.")
    assert materially_different(a, c) is True


def test_one_side_unstructured_is_always_material():
    assert materially_different(_result(structured=True), _result(structured=False, raw_text="prose")) is True


class _FakeProvider(AssistantProvider):
    name = "fake"

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.call_count = 0

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        self.call_count += 1
        text = self._replies[min(self.call_count - 1, len(self._replies) - 1)]
        return AssistantReply(text=text, provider=self.name)


_STRUCTURED_BULLISH = """{
  "instrument": "XAUUSD", "timeframe": "15M", "current_price_context": "near highs",
  "supply_zone": "4550", "demand_zone": "4500", "recent_price_sequence": "up",
  "at_meaningful_location": true, "market_context": "test", "key_levels": ["Resistance: 4550"],
  "possible_setup": null, "invalidation": "Break below 4500",
  "risk_notes": "Macro/event risk has not been checked in this chart-only analysis.",
  "bias": "Bullish", "what_i_see": "test", "setup": "Valid", "action": "Potential setup"
}"""

_STRUCTURED_BEARISH = """{
  "instrument": "XAUUSD", "timeframe": "15M", "current_price_context": "near lows",
  "supply_zone": "4550", "demand_zone": "4500", "recent_price_sequence": "down",
  "at_meaningful_location": true, "market_context": "test", "key_levels": ["Support: 4500"],
  "possible_setup": null, "invalidation": "Break above 4550",
  "risk_notes": "Macro/event risk has not been checked in this chart-only analysis.",
  "bias": "Bearish", "what_i_see": "test", "setup": "Valid", "action": "Watch"
}"""


def _bmp_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color=(1, 2, 3)).save(buf, format="BMP")
    return buf.getvalue()


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "deep_verify_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


async def test_run_deep_verification_updates_state_on_material_difference(conn):
    store = HotChartStateStore(conn)
    provider = _FakeProvider([_STRUCTURED_BEARISH])
    service = ChartAnalysisService(provider)

    served = _result(bias="Bullish", setup="Valid", action="Potential setup")

    await run_deep_verification(
        window_id="hwnd-1",
        image_bytes=_bmp_bytes(),
        image_format="image/bmp",
        chart_analysis_service=service,
        hot_state_store=store,
        served_result=_as_dict(served),
    )

    stored = await store.get_latest_for_window("hwnd-1")
    assert stored is not None
    assert stored.analysis.bias == "Bearish"


async def test_run_deep_verification_does_not_write_when_not_materially_different(conn):
    store = HotChartStateStore(conn)
    provider = _FakeProvider([_STRUCTURED_BULLISH])
    service = ChartAnalysisService(provider)

    served = _result(
        bias="Bullish",
        setup="Valid",
        action="Potential setup",
        invalidation="Break below 4500",
        key_levels=["Resistance: 4550"],
    )

    await run_deep_verification(
        window_id="hwnd-1",
        image_bytes=_bmp_bytes(),
        image_format="image/bmp",
        chart_analysis_service=service,
        hot_state_store=store,
        served_result=_as_dict(served),
    )

    assert await store.get_latest_for_window("hwnd-1") is None


async def test_run_deep_verification_never_raises_on_provider_error(conn):
    class _ExplodingProvider(AssistantProvider):
        name = "exploding"

        async def respond(self, request: AssistantRequest) -> AssistantReply:
            raise RuntimeError("provider exploded")

    store = HotChartStateStore(conn)
    service = ChartAnalysisService(_ExplodingProvider())
    served = _result()

    await run_deep_verification(
        window_id="hwnd-1",
        image_bytes=_bmp_bytes(),
        image_format="image/bmp",
        chart_analysis_service=service,
        hot_state_store=store,
        served_result=_as_dict(served),
    )

    assert await store.get_latest_for_window("hwnd-1") is None


def _as_dict(result: ChartAnalysisResult) -> dict:
    # Matches what the router actually passes at runtime
    # (fast_response.result, built via ChartAnalysisResult.to_dict()) --
    # that adds speech_text/formatted_tars_text on top of the dataclass's
    # own fields, which a plain dataclasses.asdict() would not catch as a
    # reconstruction hazard. See _result_from_served_dict in
    # deep_verification.py, which exists specifically to strip those back
    # out before rebuilding a ChartAnalysisResult.
    return result.to_dict()
