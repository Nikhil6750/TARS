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


def test_invalidation_preserves_prior_history_as_distinct_events(client):
    """Regression test for the certification blocker where invalidating a
    setup reused the original SETUP_VALID event's event_id, so the
    INSERT-OR-REPLACE persistence layer silently overwrote (destroyed) the
    SETUP_VALID row instead of appending a new SETUP_INVALIDATED one.

    SETUP_DEVELOPING -> SETUP_VALID -> SETUP_INVALIDATED must produce three
    distinct historical events, each with its own event_id, with
    SETUP_VALID still present in history afterward and active state cleared.
    """
    developing = client.post(
        "/api/v1/dev/mock-event",
        json={
            "symbol": "GBPUSD",
            "state": "SETUP_DEVELOPING",
            "validation_status": "PENDING",
            "direction": "LONG",
        },
    ).json()

    valid = client.post(
        "/api/v1/dev/mock-event",
        json={
            "symbol": "GBPUSD",
            "state": "SETUP_VALID",
            "validation_status": "VALID",
            "direction": "LONG",
            "entry": 1.27,
            "stop_loss": 1.265,
            "take_profit": 1.28,
            "risk_reward": 2.0,
        },
    ).json()

    active = client.get("/api/v1/events/active").json()
    assert len(active) == 1
    assert active[0]["event_id"] == valid["event_id"]

    invalidate_resp = client.post(f"/api/v1/events/{valid['event_id']}/invalidate")
    assert invalidate_resp.status_code == 200
    invalidated = invalidate_resp.json()

    # Every lifecycle event has its own unique event_id.
    event_ids = {developing["event_id"], valid["event_id"], invalidated["event_id"]}
    assert len(event_ids) == 3

    # Active state cleared cleanly.
    assert client.get("/api/v1/events/active").json() == []

    # All three prior events remain permanently visible in history — none
    # were overwritten or destroyed.
    history = client.get("/api/v1/events/history").json()
    history_by_id = {event["event_id"]: event for event in history}
    assert developing["event_id"] in history_by_id
    assert valid["event_id"] in history_by_id
    assert invalidated["event_id"] in history_by_id

    assert history_by_id[valid["event_id"]]["state"] == "SETUP_VALID"
    assert history_by_id[valid["event_id"]]["validation_status"] == "VALID"
    assert history_by_id[invalidated["event_id"]]["state"] == "SETUP_INVALIDATED"
    assert history_by_id[invalidated["event_id"]]["validation_status"] == "INVALID"

    # The invalidation event correlates back to the original for audit
    # purposes without reusing its event_id.
    assert any(
        code == f"ORIGINAL_EVENT_ID:{valid['event_id']}"
        for code in history_by_id[invalidated["event_id"]]["reason_codes"]
    )


def test_invalidate_unknown_event_id_returns_404(client):
    resp = client.post("/api/v1/events/00000000-0000-0000-0000-000000000000/invalidate")
    assert resp.status_code == 404


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
