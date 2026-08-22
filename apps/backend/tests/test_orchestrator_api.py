from __future__ import annotations


def test_remember_command_is_deterministic_and_persists(client):
    resp = client.post(
        "/api/v1/assistant/query", json={"text": "TARS, remember that I risk 1% per trade"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "deterministic"
    assert body["intent"] == "TOOL_TASK"
    assert "risk 1% per trade" in body["display_text"]

    # Now searchable through the ordinary search route.
    search = client.get("/api/v1/memory/search", params={"q": "risk 1%"})
    assert search.status_code == 200
    assert any(r["source"] == "explicit_memory" for r in search.json())


def test_trading_context_reports_not_configured(client):
    resp = client.post("/api/v1/assistant/query", json={"text": "what's my trading context?"})
    body = resp.json()
    assert body["provider"] == "deterministic"
    assert body["intent"] == "TOOL_TASK"
    assert "NOT_CONFIGURED" in body["display_text"]


def test_explain_setup_for_unknown_symbol(client):
    resp = client.post("/api/v1/assistant/query", json={"text": "explain the setup for ES"})
    body = resp.json()
    assert body["intent"] == "TOOL_TASK"
    assert "no active setup" in body["display_text"].lower()


def test_save_and_search_trading_observation_via_text(client):
    save = client.post(
        "/api/v1/assistant/query",
        json={"text": "TARS, save this trading observation: gold broke structure at 2400"},
    )
    assert save.json()["intent"] == "TOOL_TASK"

    search = client.post(
        "/api/v1/assistant/query", json={"text": "search trading memory for structure"}
    )
    assert search.json()["intent"] == "TOOL_TASK"
    assert "structure" in search.json()["display_text"].lower()


def test_ordinary_conversation_still_falls_through_to_provider(client):
    resp = client.post("/api/v1/assistant/query", json={"text": "hello there"})
    body = resp.json()
    assert body["provider"] == "mock"
    assert body["intent"] == "NORMAL_CONVERSATION"
    assert "canned response" in body["display_text"]


def test_active_setups_query_still_routes_through_assistant_router(client):
    client.post(
        "/api/v1/dev/mock-event",
        json={"symbol": "ES", "state": "SETUP_VALID", "validation_status": "VALID"},
    )
    resp = client.post("/api/v1/assistant/query", json={"text": "show active setups"})
    body = resp.json()
    assert body["provider"] == "deterministic"
    assert body["intent"] == "DETERMINISTIC"
