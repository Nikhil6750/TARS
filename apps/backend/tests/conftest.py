from __future__ import annotations

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("USE_MOCK_TRADING_EVENTS", "false")
    monkeypatch.setenv("ASSISTANT_PROVIDER", "mock")
    monkeypatch.setenv("STT_PROVIDER", "mock")
    monkeypatch.setenv("TTS_PROVIDER", "mock")
    monkeypatch.setenv("WAKE_WORD_PROVIDER", "mock")
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(tmp_path / "vault"))

    from app.config import get_settings

    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c

    get_settings.cache_clear()
