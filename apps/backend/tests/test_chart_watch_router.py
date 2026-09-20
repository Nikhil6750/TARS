from __future__ import annotations

import base64
import io

from PIL import Image


def _bmp_base64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color=(1, 2, 3)).save(buf, format="BMP")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_chart_watch_frame_endpoint_rejects_missing_chart_window_id(client):
    resp = client.post(
        "/api/v1/chart-watch/frame",
        json={"image_data_base64": _bmp_base64()},
    )
    assert resp.status_code == 422


def test_chart_watch_frame_endpoint_rejects_invalid_base64(client):
    resp = client.post(
        "/api/v1/chart-watch/frame",
        json={"chart_window_id": "hwnd-1", "image_data_base64": "not-base64!!"},
    )
    assert resp.status_code == 422


def test_chart_watch_frame_endpoint_accepts_a_real_frame(client, monkeypatch):
    async def fake_handle_frame(self, **kwargs):
        from assistant.chart_watch import ChartWatchOutcome
        from assistant.hot_chart_state import ChartIdentity

        return ChartWatchOutcome(
            action="refreshed",
            chart_window_id=kwargs["chart_window_id"],
            identity=ChartIdentity(chart_window_id=kwargs["chart_window_id"], symbol="XAUUSD", timeframe="15M"),
        )

    from assistant.chart_watch import ChartWatchService

    monkeypatch.setattr(ChartWatchService, "handle_frame", fake_handle_frame)

    resp = client.post(
        "/api/v1/chart-watch/frame",
        json={
            "chart_window_id": "hwnd-1",
            "image_data_base64": _bmp_base64(),
            "trigger_reason": "visual_change",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "refreshed"
    assert body["identity"]["symbol"] == "XAUUSD"
