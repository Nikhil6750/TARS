from __future__ import annotations

import time


def test_reindex_vault_endpoint_handles_missing_vault(client):
    resp = client.post("/api/v1/memory/reindex-vault")
    assert resp.status_code == 200
    body = resp.json()
    assert body["vault_missing"] is True
    assert body["indexed"] == 0


def test_search_endpoint_finds_indexed_conversation_turns(client):
    client.post("/api/v1/assistant/query", json={"text": "tell me about breakout strategies"})
    deadline = time.monotonic() + 1
    results = []
    while time.monotonic() < deadline and not results:
        resp = client.get("/api/v1/memory/search", params={"q": "breakout"})
        results = resp.json()
        time.sleep(0.01)
    assert resp.status_code == 200
    assert len(results) >= 1
    assert results[0]["source"] == "conversation"
    assert "source_id" in results[0]


def test_search_endpoint_source_filter_rejects_invalid_value(client):
    resp = client.get("/api/v1/memory/search", params={"q": "breakout", "source": "trading_events"})
    assert resp.status_code == 422
