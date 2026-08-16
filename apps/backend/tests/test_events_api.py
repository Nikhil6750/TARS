from __future__ import annotations


def test_health(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"


def test_mock_event_persists_and_lists(client):
    resp = client.post(
        "/api/v1/dev/mock-event",
        json={"symbol": "xauusd", "state": "IDLE", "validation_status": "PENDING"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["symbol"] == "XAUUSD"
    assert body["source"] == "manual"

    history = client.get("/api/v1/events").json()
    assert len(history) == 1
    assert history[0]["symbol"] == "XAUUSD"


def test_setup_valid_becomes_active(client):
    client.post(
        "/api/v1/dev/mock-event",
        json={
            "symbol": "ES",
            "state": "SETUP_VALID",
            "validation_status": "VALID",
            "direction": "LONG",
            "entry": 5300.0,
            "stop_loss": 5290.0,
            "take_profit": 5320.0,
            "risk_reward": 2.0,
        },
    )
    active = client.get("/api/v1/events/active").json()
    assert len(active) == 1
    assert active[0]["symbol"] == "ES"
    assert active[0]["state"] == "SETUP_VALID"


def test_invalidation_clears_active_setup(client):
    client.post(
        "/api/v1/dev/mock-event",
        json={
            "symbol": "ES",
            "state": "SETUP_VALID",
            "validation_status": "VALID",
        },
    )
    assert len(client.get("/api/v1/events/active").json()) == 1

    client.post(
        "/api/v1/dev/mock-event",
        json={
            "symbol": "ES",
            "state": "SETUP_INVALIDATED",
            "validation_status": "INVALID",
            "reason_codes": ["MANUAL_TEST_INVALIDATION"],
        },
    )
    assert client.get("/api/v1/events/active").json() == []


def test_risk_warning_does_not_clear_existing_active_setup(client):
    client.post(
        "/api/v1/dev/mock-event",
        json={"symbol": "ES", "state": "SETUP_VALID", "validation_status": "VALID"},
    )
    client.post(
        "/api/v1/dev/mock-event",
        json={
            "symbol": "ES",
            "state": "RISK_WARNING",
            "validation_status": "PENDING",
            "warnings": ["elevated volatility"],
        },
    )
    active = client.get("/api/v1/events/active").json()
    assert len(active) == 1
    assert active[0]["state"] == "SETUP_VALID"


def test_rejects_ai_confidence_field(client):
    resp = client.post(
        "/api/v1/dev/mock-event",
        json={
            "symbol": "ES",
            "state": "IDLE",
            "validation_status": "PENDING",
            "ai_confidence": 0.9,
        },
    )
    # Pydantic model has no such field; FastAPI ignores unknown request
    # fields by default rather than erroring, but the persisted/broadcast
    # event must never contain it regardless.
    assert resp.status_code == 201
    assert "ai_confidence" not in resp.json()
