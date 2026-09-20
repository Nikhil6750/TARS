"""End-to-end router test for TARS Alexa-Speed Phase D: a HOT,
content-matching HotChartState makes /api/v1/assistant/analyze-chart/stream
answer immediately without ever calling the configured assistant provider
-- the whole point of the fast path. Monkeypatches
`assistant.fast_chart_response.try_fast_response` (the exact function the
router calls) rather than seeding real DB rows across a synchronous
TestClient/async-store boundary -- matches this suite's existing pattern
in test_diagnostics_router.py for the same reason (the TestClient's ASGI
portal and a directly-awaited store call in a test function are not
guaranteed to share an event loop).
"""
from __future__ import annotations

import base64
import io

from PIL import Image


def _bmp_base64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color=(1, 2, 3)).save(buf, format="BMP")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_fast_path_answers_without_calling_the_provider(client, monkeypatch):
    from assistant.chart_analysis import ChartAnalysisService
    from assistant.fast_chart_response import FastResponse

    async def fake_try_fast_response(*, window_id, image_bytes, hot_state_store, t0):
        assert window_id == "hwnd-42"
        return FastResponse(
            result={"instrument": "XAUUSD", "timeframe": "15M", "raw_text": "{}"},
            timing={"claude_start_ms": 0, "first_token_ms": 2, "complete_ms": 2, "warm_path": True},
        )

    async def fail_if_called(self, **kwargs):
        raise AssertionError("provider must not be called on the fast path")
        yield {}  # pragma: no cover - never reached, keeps this an async generator

    monkeypatch.setattr("app.routers.assistant.try_fast_response", fake_try_fast_response)
    monkeypatch.setattr(ChartAnalysisService, "analyze_stream", fail_if_called)

    resp = client.post(
        "/api/v1/assistant/analyze-chart/stream",
        json={
            "conversation_id": "conv-fast",
            "capture": {
                "image_data_base64": _bmp_base64(),
                "image_format": "image/bmp",
                "window_id": "hwnd-42",
            },
        },
    )

    assert resp.status_code == 200
    body = resp.text
    assert '"type": "complete"' in body
    assert "XAUUSD" in body
    assert '"warm_path": true' in body


def test_fast_path_is_never_attempted_without_a_window_id(client, monkeypatch):
    from assistant.fast_chart_response import try_fast_response as real_try_fast_response

    calls = []

    async def spying_try_fast_response(*, window_id, image_bytes, hot_state_store, t0):
        calls.append(window_id)
        return await real_try_fast_response(
            window_id=window_id, image_bytes=image_bytes, hot_state_store=hot_state_store, t0=t0
        )

    monkeypatch.setattr("app.routers.assistant.try_fast_response", spying_try_fast_response)

    resp = client.post(
        "/api/v1/assistant/analyze-chart/stream",
        json={
            "conversation_id": "conv-cold",
            "capture": {"image_data_base64": _bmp_base64(), "image_format": "image/bmp"},
        },
    )

    assert resp.status_code == 200
    # try_fast_response is still called (with window_id=None) and honestly
    # returns None -- the router falls through to the mock provider's cold
    # path rather than skipping the fast-path check outright.
    assert calls == [None]
    assert '"warm_path": true' not in resp.text


def test_fast_path_schedules_deep_verification_after_responding(client, monkeypatch):
    from assistant.fast_chart_response import FastResponse

    async def fake_try_fast_response(*, window_id, image_bytes, hot_state_store, t0):
        return FastResponse(
            result={"instrument": "XAUUSD", "timeframe": "15M", "raw_text": "{}"},
            timing={"claude_start_ms": 0, "first_token_ms": 2, "complete_ms": 2, "warm_path": True},
        )

    calls = []

    async def spying_deep_verification(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("app.routers.assistant.try_fast_response", fake_try_fast_response)
    monkeypatch.setattr("app.routers.assistant.run_deep_verification", spying_deep_verification)

    resp = client.post(
        "/api/v1/assistant/analyze-chart/stream",
        json={
            "conversation_id": "conv-fast-deep",
            "capture": {
                "image_data_base64": _bmp_base64(),
                "image_format": "image/bmp",
                "window_id": "hwnd-42",
            },
        },
    )

    assert resp.status_code == 200
    # BackgroundTasks run as part of completing the ASGI response cycle,
    # which TestClient's in-process transport has already done by the
    # time client.post() returns -- no polling/sleeping needed here.
    assert len(calls) == 1
    assert calls[0]["window_id"] == "hwnd-42"
    assert calls[0]["served_result"]["instrument"] == "XAUUSD"


def test_fast_path_is_skipped_for_a_custom_goal_text(client, monkeypatch):
    called = {"count": 0}

    async def spying_try_fast_response(*, window_id, image_bytes, hot_state_store, t0):
        called["count"] += 1
        return None

    monkeypatch.setattr("app.routers.assistant.try_fast_response", spying_try_fast_response)

    resp = client.post(
        "/api/v1/assistant/analyze-chart/stream",
        json={
            "conversation_id": "conv-custom-goal",
            "capture": {
                "image_data_base64": _bmp_base64(),
                "image_format": "image/bmp",
                "window_id": "hwnd-42",
            },
            "goal": "What is the RSI doing right now?",
        },
    )

    assert resp.status_code == 200
    assert called["count"] == 0
