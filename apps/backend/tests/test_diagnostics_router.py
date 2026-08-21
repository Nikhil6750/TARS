from __future__ import annotations


def test_latency_diagnostics_endpoint_returns_empty_stats_with_no_data(client):
    resp = client.get("/api/v1/diagnostics/latency?kind=chart_analysis")
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "chart_analysis"
    assert body["sample_size"] == 0
    assert body["p50_ms"] is None


def test_latency_diagnostics_endpoint_reflects_a_real_chart_analysis_request(client, monkeypatch):
    async def fake_analyze_stream(self, **kwargs):
        yield {"type": "status", "text": "Looking at the chart..."}
        yield {
            "type": "complete",
            "result": {"instrument": "XAUUSD"},
            "timing": {"claude_start_ms": 5, "first_token_ms": 100, "complete_ms": 500},
        }
        await self._record_trace(
            request_id="manual-test",
            conversation_id=str(kwargs.get("conversation_id")),
            started_at="2026-08-20T00:00:00Z",
            t0=0.0,
            capture_ms=kwargs.get("capture_ms"),
            provider_id="claude_code",
            total_ms=500,
        )

    from assistant.chart_analysis import ChartAnalysisService

    monkeypatch.setattr(ChartAnalysisService, "analyze_stream", fake_analyze_stream)

    resp = client.post(
        "/api/v1/assistant/analyze-chart/stream",
        json={
            "conversation_id": "conv-diag",
            "capture": {"image_data_base64": "aGVsbG8="},
            "capture_ms": 250,
        },
    )
    assert resp.status_code == 200

    diag = client.get("/api/v1/diagnostics/latency?kind=chart_analysis")
    body = diag.json()
    assert body["sample_size"] == 1
    assert body["p50_ms"] == 500


def test_provider_health_endpoint_returns_empty_stats_for_an_unknown_provider(client):
    resp = client.get("/api/v1/diagnostics/provider-health?provider_id=nobody_ever_called_this")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider_id"] == "nobody_ever_called_this"
    assert body["sample_size"] == 0
    assert body["success_rate"] == 0.0


def test_provider_health_endpoint_reflects_a_real_text_request(client):
    resp = client.post(
        "/api/v1/assistant/query",
        json={"text": "hello there, how are you today", "conversation_id": None},
    )
    assert resp.status_code == 200

    diag = client.get("/api/v1/diagnostics/provider-health?provider_id=mock&kind=assistant_text")
    assert diag.status_code == 200
    body = diag.json()
    assert body["sample_size"] == 1
    assert body["success_rate"] == 1.0
    assert body["error_count"] == 0
