from __future__ import annotations


def test_generic_question_uses_configured_provider(client):
    resp = client.post("/api/v1/assistant/query", json={"text": "hello there"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "assistant"
    assert body["providers"]["assistant"] == "mock"
    assert "canned response" in body["content"]


def test_active_setups_query_is_deterministic_and_never_calls_provider(client):
    client.post(
        "/api/v1/dev/mock-event",
        json={"symbol": "ES", "state": "SETUP_VALID", "validation_status": "VALID"},
    )
    resp = client.post("/api/v1/assistant/query", json={"text": "show active setups"})
    body = resp.json()
    assert body["providers"]["assistant"] == "deterministic"
    assert body["intent"] == "show_active_setups"
    assert "ES" in body["content"]


def test_attention_query_is_deterministic(client):
    resp = client.post(
        "/api/v1/assistant/query", json={"text": "what requires my attention?"}
    )
    body = resp.json()
    assert body["providers"]["assistant"] == "deterministic"
    assert body["intent"] == "attention_summary"


def test_conversation_id_persists_across_turns(client):
    first = client.post("/api/v1/assistant/query", json={"text": "hello"}).json()
    conversation_id = first["conversation_id"]
    second = client.post(
        "/api/v1/assistant/query",
        json={"text": "hello again", "conversation_id": conversation_id},
    ).json()
    assert second["conversation_id"] == conversation_id


def test_assistant_message_matches_contract_shape(client):
    body = client.post("/api/v1/assistant/query", json={"text": "hi"}).json()
    for field in (
        "schema_version",
        "message_id",
        "conversation_id",
        "timestamp",
        "role",
        "content",
        "input_mode",
        "providers",
    ):
        assert field in body
    assert body["schema_version"] == "1.0.0"
