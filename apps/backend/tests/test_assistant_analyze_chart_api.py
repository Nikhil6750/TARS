from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from app.deps import get_chart_analysis_service
from assistant.chart_analysis import ChartAnalysisResult
from assistant.errors import AssistantProviderError


def _png_data_uri() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color=(1, 2, 3)).save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{encoded}"


class _StubChartAnalysisService:
    def __init__(self, result: ChartAnalysisResult | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    async def analyze(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _sample_result() -> ChartAnalysisResult:
    return ChartAnalysisResult(
        instrument="EURUSD",
        timeframe="4H",
        market_context="Consolidating below a prior high.",
        key_levels=["1.0950"],
        possible_setup="Possible continuation on a breakout.",
        invalidation="a close below 1.0870",
        risk_notes="Limited visible history.",
        provider="claude_code",
        raw_text="{...}",
        structured=True,
    )


@pytest.fixture
def override_chart_service(client):
    def _install(stub: _StubChartAnalysisService):
        client.app.dependency_overrides[get_chart_analysis_service] = lambda: stub
        return stub

    yield _install
    client.app.dependency_overrides.pop(get_chart_analysis_service, None)


def test_analyze_chart_returns_structured_result(client, override_chart_service):
    stub = override_chart_service(_StubChartAnalysisService(result=_sample_result()))

    resp = client.post(
        "/api/v1/assistant/analyze-chart",
        json={
            "capture": {"image_data_base64": _png_data_uri(), "image_format": "image/png"},
            "active_context": {"executable": "chrome.exe", "window_title": "TradingView"},
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["instrument"] == "EURUSD"
    assert body["structured"] is True
    assert "quant_brain" in body["disclaimer"]
    assert body["speech_text"]
    assert "%" not in body["speech_text"]
    assert len(stub.calls) == 1
    assert "TradingView" in stub.calls[0]["active_context_text"]


def test_analyze_chart_rejects_missing_capture(client, override_chart_service):
    override_chart_service(_StubChartAnalysisService(result=_sample_result()))
    resp = client.post("/api/v1/assistant/analyze-chart", json={})
    assert resp.status_code == 422


def test_analyze_chart_rejects_invalid_base64(client, override_chart_service):
    override_chart_service(_StubChartAnalysisService(result=_sample_result()))
    resp = client.post(
        "/api/v1/assistant/analyze-chart",
        json={"capture": {"image_data_base64": "not-base64-!!!", "image_format": "image/png"}},
    )
    assert resp.status_code == 422


def test_analyze_chart_rejects_secure_desktop_capture(client, override_chart_service):
    override_chart_service(_StubChartAnalysisService(result=_sample_result()))
    resp = client.post(
        "/api/v1/assistant/analyze-chart",
        json={
            "capture": {
                "image_data_base64": _png_data_uri(),
                "image_format": "image/png",
                "is_secure_desktop": True,
            }
        },
    )
    assert resp.status_code == 422


def test_analyze_chart_rejects_capture_error(client, override_chart_service):
    override_chart_service(_StubChartAnalysisService(result=_sample_result()))
    resp = client.post(
        "/api/v1/assistant/analyze-chart",
        json={
            "capture": {
                "image_data_base64": _png_data_uri(),
                "image_format": "image/png",
                "error": "No foreground window active",
            }
        },
    )
    assert resp.status_code == 422


def test_analyze_chart_surfaces_provider_failure_as_502_never_fabricated_success(
    client, override_chart_service
):
    override_chart_service(
        _StubChartAnalysisService(error=AssistantProviderError("claude CLI not found"))
    )
    resp = client.post(
        "/api/v1/assistant/analyze-chart",
        json={"capture": {"image_data_base64": _png_data_uri(), "image_format": "image/png"}},
    )
    assert resp.status_code == 502
    assert "claude CLI not found" in resp.json()["detail"]
