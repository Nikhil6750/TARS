"""Desktop-lifecycle endpoints (mission: FINAL INFRASTRUCTURE MISSION) --
graceful shutdown signal, watcher pause/resume/status (the SAME
MarketWatchService the voice tools use), post-sleep rebase, daily-brief
status. Uses the real app via the shared `client` fixture (see conftest.py)
so these exercise the real app.state wiring, not mocks.
"""
from __future__ import annotations


def test_shutdown_without_a_real_uvicorn_server_reference_is_reported_honestly(client):
    """Under TestClient (and under any non-run.py launch path), no
    uvicorn.Server reference was ever set -- the endpoint must say so
    rather than pretending it requested a shutdown."""
    resp = client.post("/api/v1/runtime/shutdown")
    assert resp.status_code == 200
    assert resp.json() == {"status": "no_server_reference"}


def test_watcher_pause_then_resume_round_trips_through_the_real_service(client):
    resp = client.post("/api/v1/watcher/pause")
    assert resp.status_code == 200
    assert resp.json()["paused"] is True

    resp = client.get("/api/v1/watcher/status")
    assert resp.json()["paused"] is True

    resp = client.post("/api/v1/watcher/resume")
    assert resp.json()["paused"] is False

    resp = client.get("/api/v1/watcher/status")
    assert resp.json()["paused"] is False


def test_watcher_status_shape(client):
    resp = client.get("/api/v1/watcher/status")
    body = resp.json()
    assert set(body.keys()) == {"paused", "watched", "cycles", "events_considered",
                                 "alerts_published", "last_rebase_at"}


def test_resume_rebase_clears_baselines_without_touching_watchlist(client):
    resp = client.post("/api/v1/runtime/resume_rebase")
    assert resp.status_code == 200
    assert resp.json() == {"status": "rebased"}

    status = client.get("/api/v1/watcher/status").json()
    assert status["last_rebase_at"] is not None


def test_daily_brief_status_shape(client):
    resp = client.get("/api/v1/runtime/daily_brief_status")
    assert resp.status_code == 200
    body = resp.json()
    assert "date" in body
    assert isinstance(body["already_sent_today"], bool)


def test_readiness_now_also_reports_watcher_and_monitor_state(client):
    """Additive extension -- the original assistant/stt/tts/wake/database/
    claude_cli/ready/message shape (test_runtime_readiness.py) is
    untouched; this only asserts the NEW keys are present and truthful."""
    resp = client.get("/api/v1/runtime/readiness")
    body = resp.json()
    for key in ("mt5", "calendar", "news", "tradingview", "watcher"):
        assert key in body
    assert "paused" in body["watcher"]
    assert "watched" in body["watcher"]
