from __future__ import annotations

from uuid import uuid4

from app.contracts import validate_assistant_message
from assistant.conversation_store import ConversationStore
from assistant.errors import AssistantProviderError
from assistant.provider import AssistantProvider, AssistantReply, AssistantRequest
from assistant.response_quality import ResponseComposer, ResponseQualityContract
from assistant.router import AssistantRouter
from events.service import EventService


def test_response_composer_separates_markdown_display_from_natural_speech():
    presentation = ResponseComposer().compose(
        user_text="Compare the options and include a table.",
        display_text=(
            "Certainly!\n### Result\n\n"
            "| Choice | Detail |\n|---|---|\n| **A** | Use `safe_mode` |\n\n"
            "See https://example.com/docs and C:\\TARS\\private\\log.txt."
        ),
    )

    assert presentation.display_text.startswith("### Result")
    assert "**A**" in presentation.display_text
    assert "Certainly" not in presentation.display_text
    assert "example.com" in presentation.speech_text
    for marker in ("**", "*", "###", "`", "|"):
        assert marker not in presentation.speech_text
    assert "https://" not in presentation.speech_text
    assert "C:\\" not in presentation.speech_text


def test_quality_contract_flags_missing_uncertainty_and_internal_leakage():
    result = ResponseQualityContract().assess(
        user_text="No portfolio history is provided. What was my drawdown?",
        display_text="The subprocess at C:\\TARS\\app exited with code 2.",
        speech_text="The subprocess failed.",
    )

    assert result.uncertainty is False
    assert result.user_mode_cleanliness is False
    assert "uncertainty" in result.issues
    assert "user_mode_cleanliness" in result.issues


def test_v1_query_is_the_only_canonical_presentation_endpoint(client):
    v1 = client.post("/api/v1/assistant/query", json={"text": "hello"}).json()
    assert v1["display_text"]
    assert v1["speech_text"]
    assert v1["intent"] == "NORMAL_CONVERSATION"

    assert client.post("/api/v2/assistant/query", json={"text": "hello"}).status_code == 404

    legacy = client.post("/api/v1/assistant/messages", json={"text": "hello"}).json()
    validate_assistant_message(legacy)


class _MarkdownProvider(AssistantProvider):
    name = "markdown"

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        return AssistantReply(
            text="### Result\nUse **composition**. See https://example.com/docs and `code`.",
            provider=self.name,
        )


def test_backend_alone_derives_plain_speech_from_markdown(client):
    client.app.state.turn_controller._provider = _MarkdownProvider()

    body = client.post("/api/v1/assistant/query", json={"text": "Explain composition"}).json()

    assert "### Result" in body["display_text"]
    assert "composition" in body["speech_text"]
    for marker in ("**", "###", "`", "https://"):
        assert marker not in body["speech_text"]


class _FailingProvider(AssistantProvider):
    name = "codex"

    async def respond(self, request: AssistantRequest):
        raise AssistantProviderError(
            "Codex CLI exited 1 at C:\\TARS\\repo: provider subprocess stderr=secret"
        )


async def test_provider_failure_returns_only_clean_user_message(client):
    app = client.app
    router = AssistantRouter(
        event_service=EventService(app.state.db.conn),
        conversation_store=ConversationStore(app.state.db.conn),
        provider=_FailingProvider(),
        memory_service=app.state.memory_service,
        trace_store=app.state.latency_trace_store,
    )

    reply = await router.handle_text("Explain this", str(uuid4()))

    assert reply.display_text == "I couldn't answer that right now. Please try again."
    assert reply.assistant_message.error == reply.display_text
    combined = f"{reply.display_text} {reply.assistant_message.error}"
    assert "Codex" not in combined
    assert "CLI" not in combined
    assert "C:\\TARS" not in combined
    assert "subprocess" not in combined
