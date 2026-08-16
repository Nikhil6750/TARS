from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def test_retrieved_source_id_survives_actual_assistant_grounding_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_id = "Research/Provenance.md"
    vault = tmp_path / "vault"
    note = vault / source_id
    note.parent.mkdir(parents=True)
    note.write_text(
        "# Provenance\n\nAURORA_SOURCE_ANCHOR belongs to this note.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'memory.db'}")
    monkeypatch.setenv("USE_MOCK_TRADING_EVENTS", "false")
    monkeypatch.setenv("ASSISTANT_PROVIDER", "mock")
    monkeypatch.setenv("STT_PROVIDER", "mock")
    monkeypatch.setenv("TTS_PROVIDER", "mock")
    monkeypatch.setenv("WAKE_WORD_PROVIDER", "mock")
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))

    from app.config import get_settings
    from app.main import create_app
    from assistant.provider import AssistantProvider, AssistantReply, AssistantRequest
    from fastapi.testclient import TestClient

    class CapturingProvider(AssistantProvider):
        name = "certification_capture"

        def __init__(self) -> None:
            self.requests: list[AssistantRequest] = []

        async def respond(self, request: AssistantRequest) -> AssistantReply:
            self.requests.append(request)
            return AssistantReply(
                text="No statistical performance data is available.",
                provider=self.name,
            )

    get_settings.cache_clear()
    app = create_app()
    provider = CapturingProvider()
    try:
        with TestClient(app) as api:
            deadline = time.monotonic() + 5.0
            while not api.get("/health").is_success:
                assert time.monotonic() < deadline
                threading.Event().wait(0.01)
            app.state.assistant_provider = provider
            response = api.post(
                "/api/v1/assistant/query",
                json={
                    "text": "AURORA_SOURCE_ANCHOR",
                    "conversation_id": "f0817378-a296-4a59-8505-bbda6b10e4ae",
                },
            )
            response.raise_for_status()

        assert len(provider.requests) == 1
        context = provider.requests[0].system_context
        assert '"source": "vault"' in context
        assert f'"source_id": "{source_id}"' in context
    finally:
        get_settings.cache_clear()
