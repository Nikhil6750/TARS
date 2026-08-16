from __future__ import annotations


def test_ws_receives_snapshot_then_broadcast(client):
    with client.websocket_connect("/ws/events") as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "active_snapshot"
        assert snapshot["events"] == []

        client.post(
            "/api/v1/dev/mock-event",
            json={"symbol": "ES", "state": "SETUP_VALID", "validation_status": "VALID"},
        )

        message = ws.receive_json()
        assert message["type"] == "trading_event"
        assert message["event"]["symbol"] == "ES"
        assert message["active_state_change"] == "upserted"
