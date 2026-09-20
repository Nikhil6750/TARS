from __future__ import annotations


def test_generic_question_uses_configured_provider(client):
    resp = client.post("/api/v1/assistant/query", json={"text": "hello there"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "NORMAL_CONVERSATION"
    assert body["provider"] == "mock"
    assert body["status"] == "completed"
    assert "canned response" in body["display_text"]


def test_active_setups_query_is_deterministic_and_never_calls_provider(client):
    client.post(
        "/api/v1/dev/mock-event",
        json={"symbol": "ES", "state": "SETUP_VALID", "validation_status": "VALID"},
    )
    resp = client.post("/api/v1/assistant/query", json={"text": "show active setups"})
    body = resp.json()
    assert body["provider"] == "deterministic"
    assert body["intent"] == "DETERMINISTIC"
    assert "ES" in body["display_text"]


def test_attention_query_is_deterministic(client):
    resp = client.post(
        "/api/v1/assistant/query", json={"text": "what requires my attention?"}
    )
    body = resp.json()
    assert body["provider"] == "deterministic"
    assert body["intent"] == "DETERMINISTIC"


def test_conversation_id_persists_across_turns(client):
    first = client.post("/api/v1/assistant/query", json={"text": "hello"}).json()
    conversation_id = first["conversation_id"]
    second = client.post(
        "/api/v1/assistant/query",
        json={"text": "hello again", "conversation_id": conversation_id},
    ).json()
    assert second["conversation_id"] == conversation_id


def test_assistant_response_has_one_canonical_shape(client):
    body = client.post("/api/v1/assistant/query", json={"text": "hi"}).json()
    for field in (
        "turn_id",
        "conversation_id",
        "display_text",
        "speech_text",
        "intent",
        "status",
        "provider",
        "latency_ms",
    ):
        assert field in body


def test_legacy_message_alias_delegates_and_projects_frozen_contract(client):
    body = client.post("/api/v1/assistant/messages", json={"text": "hi"}).json()
    assert body["schema_version"] == "1.0.0"
    assert body["role"] == "assistant"
    assert body["providers"]["assistant"] == "mock"


def test_same_turn_id_cannot_execute_different_text(client):
    turn_id = "fixed-api-turn"
    first = client.post(
        "/api/v1/assistant/query", json={"turn_id": turn_id, "text": "hello"}
    )
    conflict = client.post(
        "/api/v1/assistant/query", json={"turn_id": turn_id, "text": "different"}
    )
    assert first.status_code == 200
    assert conflict.status_code == 409
