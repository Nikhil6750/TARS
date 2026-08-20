from __future__ import annotations

import io

import aiosqlite
import pytest
from PIL import Image

from app.latency_store import LatencyTraceStore
from assistant.chart_analysis import ChartAnalysisError, ChartAnalysisService
from assistant.provider import AssistantProvider, AssistantReply, AssistantRequest
from storage.migrator import run_migrations

_STRUCTURED_JSON = """{
  "instrument": "EURUSD",
  "timeframe": "4H",
  "current_price_context": "1.0920, mid-range",
  "supply_zone": "1.0950",
  "demand_zone": "1.0870",
  "recent_price_sequence": "Pulled back from 1.0950, now consolidating.",
  "at_meaningful_location": true,
  "market_context": "Price is consolidating below a prior swing high.",
  "key_levels": ["1.0950 resistance", "1.0870 support"],
  "possible_setup": "A break and retest of 1.0950 could favor continuation long.",
  "invalidation": "a daily close below 1.0870",
  "risk_notes": "Low sample of visible candles; no higher-timeframe context shown."
}"""


class _FakeProvider(AssistantProvider):
    name = "fake"

    def __init__(self, reply_text: str) -> None:
        self.reply_text = reply_text
        self.last_request: AssistantRequest | None = None
        # Captured while the temp image file still exists -- the service
        # deletes its temp directory as soon as respond() returns.
        self.seen_image_path: str | None = None
        self.seen_image_format: str | None = None

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        self.last_request = request
        if request.image_path:
            self.seen_image_path = request.image_path
            with Image.open(request.image_path) as img:
                self.seen_image_format = img.format
        return AssistantReply(text=self.reply_text, provider=self.name)


class _FakeStreamingProvider(AssistantProvider):
    name = "fake_stream"

    def __init__(self, reply_text: str, *, raise_error: bool = False) -> None:
        self.reply_text = reply_text
        self.raise_error = raise_error

    async def respond(self, request: AssistantRequest) -> AssistantReply:  # pragma: no cover - unused in stream tests
        return AssistantReply(text=self.reply_text, provider=self.name)

    async def respond_stream(self, request: AssistantRequest):
        yield {"type": "status", "text": "Reading the chart..."}
        if self.raise_error:
            raise RuntimeError("provider exploded")
        yield {"type": "delta", "text": self.reply_text[:5]}
        yield {"type": "delta", "text": self.reply_text[5:]}
        yield {"type": "complete", "text": self.reply_text, "provider": self.name}


def _bmp_bytes(width: int = 4, height: int = 4) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(10, 20, 30)).save(buf, format="BMP")
    return buf.getvalue()


@pytest.fixture
async def trace_conn(tmp_path):
    db_path = tmp_path / "chart_trace_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


async def test_analyze_parses_structured_json_reply():
    provider = _FakeProvider(_STRUCTURED_JSON)
    service = ChartAnalysisService(provider)

    result = await service.analyze(
        image_bytes=_bmp_bytes(),
        image_format="image/bmp",
        conversation_id="conv-1",
    )

    assert result.structured is True
    assert result.instrument == "EURUSD"
    assert result.timeframe == "4H"
    assert result.key_levels == ["1.0950 resistance", "1.0870 support"]
    assert "consolidating" in result.market_context
    assert result.possible_setup is not None
    assert result.invalidation is not None
    assert result.risk_notes
    assert "not a quant_brain-validated" in result.disclaimer
    assert result.current_price_context == "1.0920, mid-range"
    assert result.supply_zone == "1.0950"
    assert result.demand_zone == "1.0870"
    assert result.recent_price_sequence
    assert result.at_meaningful_location is True
    formatted = result.formatted_tars_text()
    assert "Current price: 1.0920, mid-range" in formatted
    assert "### STRUCTURE" in formatted
    assert "### KEY LEVELS" in formatted
    assert formatted.index("### STRUCTURE") < formatted.index("BIAS:")
    assert formatted.index("BIAS:") < formatted.index("### KEY LEVELS")


async def test_analyze_falls_back_to_raw_text_when_reply_is_not_structured():
    provider = _FakeProvider("I can see a candlestick chart but can't make out any labels.")
    service = ChartAnalysisService(provider)

    result = await service.analyze(
        image_bytes=_bmp_bytes(), image_format="image/bmp", conversation_id="conv-2"
    )

    assert result.structured is False
    assert result.instrument is None
    assert result.timeframe is None
    assert "candlestick" in result.market_context
    assert result.key_levels == []
    assert "not returned in the requested structured format" in result.risk_notes
    # Unstructured fallback must not fabricate a "Bias is Neutral" claim
    # that was never actually parsed from the model.
    assert "Bias is" not in result.speech_text()
    assert "candlestick" in result.speech_text()


async def test_unstructured_fallback_speech_text_strips_markdown():
    provider = _FakeProvider(
        "**XAUUSD — 15m**\n\n### What I see\n- Price at 4,351\n- *tight range* near resistance"
    )
    service = ChartAnalysisService(provider)

    result = await service.analyze(
        image_bytes=_bmp_bytes(), image_format="image/bmp", conversation_id="conv-md"
    )

    speech = result.speech_text()
    assert "*" not in speech
    assert "#" not in speech
    assert "XAUUSD" in speech
    assert "tight range" in speech


async def test_analyze_never_fabricates_confidence_or_certainty_language():
    provider = _FakeProvider(_STRUCTURED_JSON)
    service = ChartAnalysisService(provider)
    result = await service.analyze(
        image_bytes=_bmp_bytes(), image_format="image/bmp", conversation_id="conv-3"
    )
    speech = result.speech_text()
    assert "guaranteed" not in speech.lower()
    assert "%" not in speech
    assert "not a quant_brain-validated" in result.disclaimer.lower()


async def test_analyze_converts_bmp_to_png_before_handing_to_provider():
    provider = _FakeProvider(_STRUCTURED_JSON)
    service = ChartAnalysisService(provider)

    await service.analyze(image_bytes=_bmp_bytes(), image_format="image/bmp", conversation_id="c")

    assert provider.seen_image_path is not None
    assert provider.seen_image_path.endswith(".png")
    assert provider.seen_image_format == "PNG"


async def test_analyze_rejects_undecodable_image_bytes():
    provider = _FakeProvider(_STRUCTURED_JSON)
    service = ChartAnalysisService(provider)

    with pytest.raises(ChartAnalysisError):
        await service.analyze(
            image_bytes=b"not an image", image_format="image/bmp", conversation_id="c"
        )
    assert provider.last_request is None


async def test_analyze_includes_active_context_in_grounding_but_not_as_chart_content():
    provider = _FakeProvider(_STRUCTURED_JSON)
    service = ChartAnalysisService(provider)

    await service.analyze(
        image_bytes=_bmp_bytes(),
        image_format="image/bmp",
        conversation_id="c",
        active_context_text="active application: chrome.exe; window title: TradingView",
    )

    assert provider.last_request is not None
    assert "TradingView" in provider.last_request.system_context
    assert "not necessarily part of the chart itself" in provider.last_request.system_context


async def test_capital_question_without_trade_params_does_not_reach_the_model():
    # Regression: asking "analyze the chart and estimate the profit if my
    # capital was 10000 rupees" together, observed live, made Claude abandon
    # the JSON schema entirely and fabricate a profit table under assumed
    # leverage. The profit half must never reach the vision call.
    provider = _FakeProvider(_STRUCTURED_JSON)
    service = ChartAnalysisService(provider)

    result = await service.analyze(
        image_bytes=_bmp_bytes(),
        image_format="image/bmp",
        conversation_id="c",
        goal_text="Analyze the chart and estimate the profit if my capital was 10000 rupees",
    )

    assert provider.last_request is not None
    assert "rupee" not in provider.last_request.text.lower()
    assert "10000" not in provider.last_request.text
    assert "profit" not in provider.last_request.text.lower()

    assert result.capital_note is not None
    assert "entry" in result.capital_note
    assert "stop loss" in result.capital_note
    assert "target" in result.capital_note
    # No fabricated currency figure anywhere the user will actually see.
    import re as _re

    assert not _re.search(r"[₹$]\s*\d|\d+\s*(rupees|inr)\b", result.formatted_tars_text(), _re.IGNORECASE)
    assert not _re.search(r"[₹$]\s*\d|\d+\s*(rupees|inr)\b", result.speech_text(), _re.IGNORECASE)
    assert "CAPITAL / PROFIT ESTIMATE" in result.formatted_tars_text()


async def test_capital_question_with_explicit_trade_params_is_not_blocked():
    # If the user actually supplies entry/stop/target, this isn't the
    # "needs more info" case -- the vision goal should pass through as-is
    # (a real deterministic calculator is future scope, not this fix).
    provider = _FakeProvider(_STRUCTURED_JSON)
    service = ChartAnalysisService(provider)

    await service.analyze(
        image_bytes=_bmp_bytes(),
        image_format="image/bmp",
        conversation_id="c",
        goal_text="Entry 4350, stop loss 4320, target 4400, estimate my profit on 10000 rupees capital",
    )

    assert provider.last_request is not None
    assert "entry" in provider.last_request.text.lower()


async def test_formatted_tars_text_never_truncates_large_unstructured_response():
    huge_text = "**Chart notes**\n\n" + ("Observed price action continues. " * 400)
    assert len(huge_text) > 5000
    provider = _FakeProvider(huge_text)
    service = ChartAnalysisService(provider)

    result = await service.analyze(
        image_bytes=_bmp_bytes(), image_format="image/bmp", conversation_id="c"
    )

    assert result.structured is False
    formatted = result.formatted_tars_text()
    assert len(formatted) >= len(huge_text.strip()) - 5
    assert formatted.strip().endswith("continues.") or formatted.rstrip().endswith(
        "Observed price action continues."
    )


async def test_formatted_tars_text_uses_markdown_headings_frontend_recognizes():
    provider = _FakeProvider(_STRUCTURED_JSON)
    service = ChartAnalysisService(provider)

    result = await service.analyze(
        image_bytes=_bmp_bytes(), image_format="image/bmp", conversation_id="c"
    )

    formatted = result.formatted_tars_text()
    for heading in ("### STRUCTURE", "### SETUP", "### KEY LEVELS", "### RISK"):
        assert heading in formatted
    # BIAS/ACTION stay single-line -- MarkdownContent.tsx's badge renderer
    # for these two extracts the value from the same line.
    assert "BIAS: Bullish" in formatted or "BIAS:" in formatted
    assert "ACTION:" in formatted
    # Bulleted, not flattened into one paragraph.
    assert "- Current price:" in formatted
    assert "- Supply zone:" in formatted


async def test_complete_result_is_not_overwritten_by_partial_provider_text():
    # The final `complete` result must reflect the FULL provider reply, not
    # some earlier/partial fragment -- respond() only ever returns once,
    # with the whole text, so there is nothing to "overwrite" it with here;
    # this pins that contract so a future streaming refactor can't regress it.
    provider = _FakeProvider(_STRUCTURED_JSON)
    service = ChartAnalysisService(provider)

    result = await service.analyze(
        image_bytes=_bmp_bytes(), image_format="image/bmp", conversation_id="c"
    )

    assert result.raw_text == _STRUCTURED_JSON
    assert result.to_dict()["raw_text"] == _STRUCTURED_JSON
    assert result.to_dict()["formatted_tars_text"] == result.formatted_tars_text()


async def test_analyze_stream_records_a_trace_on_success(trace_conn):
    trace_store = LatencyTraceStore(trace_conn)
    provider = _FakeStreamingProvider(_STRUCTURED_JSON)
    service = ChartAnalysisService(provider, trace_store=trace_store)

    events = [
        item
        async for item in service.analyze_stream(
            image_bytes=_bmp_bytes(),
            image_format="image/bmp",
            conversation_id="conv-stream",
            capture_ms=312.0,
        )
    ]

    assert events[-1]["type"] == "complete"
    assert events[-1]["result"]["instrument"] == "EURUSD"

    rows = await trace_store.recent("chart_analysis")
    assert len(rows) == 1
    row = rows[0]
    assert row["conversation_id"] == "conv-stream"
    assert row["provider_id"] == "fake_stream"
    assert row["capture_ms"] == 312.0
    assert row["error"] is None
    assert row["total_ms"] is not None and row["total_ms"] >= 0


async def test_analyze_stream_records_a_trace_on_provider_error(trace_conn):
    trace_store = LatencyTraceStore(trace_conn)
    provider = _FakeStreamingProvider(_STRUCTURED_JSON, raise_error=True)
    service = ChartAnalysisService(provider, trace_store=trace_store)

    with pytest.raises(RuntimeError, match="provider exploded"):
        async for _ in service.analyze_stream(
            image_bytes=_bmp_bytes(),
            image_format="image/bmp",
            conversation_id="conv-err",
        ):
            pass

    rows = await trace_store.recent("chart_analysis")
    assert len(rows) == 1
    assert rows[0]["error"] is not None
    assert "provider exploded" in rows[0]["error"]


async def test_analyze_stream_records_a_trace_on_undecodable_image(trace_conn):
    trace_store = LatencyTraceStore(trace_conn)
    provider = _FakeStreamingProvider(_STRUCTURED_JSON)
    service = ChartAnalysisService(provider, trace_store=trace_store)

    with pytest.raises(ChartAnalysisError):
        async for _ in service.analyze_stream(
            image_bytes=b"not an image",
            image_format="image/bmp",
            conversation_id="conv-decode",
        ):
            pass

    rows = await trace_store.recent("chart_analysis")
    assert len(rows) == 1
    assert "decode_failed" in rows[0]["error"]


async def test_analyze_stream_without_trace_store_does_not_error():
    # trace_store is optional -- existing callers/tests that never wire one
    # up must keep working exactly as before.
    provider = _FakeStreamingProvider(_STRUCTURED_JSON)
    service = ChartAnalysisService(provider)

    events = [
        item
        async for item in service.analyze_stream(
            image_bytes=_bmp_bytes(), image_format="image/bmp", conversation_id="conv-no-trace"
        )
    ]
    assert events[-1]["type"] == "complete"
