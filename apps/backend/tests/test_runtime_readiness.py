from __future__ import annotations


def test_readiness_reports_not_ready_when_providers_are_mocked(client):
    """The `client` fixture (see conftest.py) configures every provider to
    'mock' -- exactly the config this endpoint exists to catch. It must
    never report ready=true just because the app booted successfully."""
    resp = client.get("/api/v1/runtime/readiness")
    assert resp.status_code == 200
    body = resp.json()

    assert body["ready"] is False
    assert body["message"]
    assert body["assistant"]["ready"] is False
    assert body["assistant"]["configured"] == "mock"
    assert body["assistant"]["expected"] == "claude_code"
    assert body["stt"]["ready"] is False
    assert body["tts"]["ready"] is False
    # Wake detection has no standalone provider -- it tracks STT readiness
    # (see readiness.py's comment on why).
    assert body["wake"]["ready"] is False
    assert body["database"]["ready"] is True


def test_readiness_response_shape_is_stable(client):
    """Every component the frontend/native side reads must always be
    present, even when not ready, so callers never have to guess."""
    resp = client.get("/api/v1/runtime/readiness")
    body = resp.json()

    for key in ("assistant", "stt", "tts", "wake", "database", "claude_cli"):
        assert key in body
        component = body[key]
        assert set(component.keys()) == {"configured", "expected", "ready", "detail"}
        assert isinstance(component["ready"], bool)
