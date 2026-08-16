from __future__ import annotations

import pytest


@pytest.fixture
def misconfigured_client(tmp_path, monkeypatch):
    """A misconfigured assistant provider (ollama with no model set) must
    never take down the whole backend — it should fall back to the mock
    provider and leave events/health fully functional."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("USE_MOCK_TRADING_EVENTS", "false")
    monkeypatch.setenv("ASSISTANT_PROVIDER", "ollama")
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(tmp_path / "vault"))

    from app.config import get_settings

    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c

    get_settings.cache_clear()


def test_misconfigured_provider_falls_back_to_mock(misconfigured_client):
    health = misconfigured_client.get("/api/v1/health")
    assert health.status_code == 200

    reply = misconfigured_client.post(
        "/api/v1/assistant/query", json={"text": "hello"}
    ).json()
    assert reply["providers"]["assistant"] == "mock"
