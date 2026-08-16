"""Optional direct Anthropic API adapter — requires a paid ANTHROPIC_API_KEY.
Never required for TARS to run; every other provider works without one. Uses
the official `anthropic` SDK, imported lazily so the package is only a hard
requirement when this specific provider is selected (see
requirements-optional.txt).
"""
from __future__ import annotations

from assistant.errors import AssistantProviderError
from assistant.provider import AssistantProvider, AssistantReply, AssistantRequest


class AnthropicAPIProvider(AssistantProvider):
    name = "anthropic_api"

    def __init__(self, api_key: str, model: str):
        if not api_key:
            raise AssistantProviderError(
                "AnthropicAPIProvider selected but ANTHROPIC_API_KEY is not set"
            )
        try:
            import anthropic
        except ImportError as exc:
            raise AssistantProviderError(
                "AnthropicAPIProvider selected but the 'anthropic' package is "
                "not installed — pip install -r requirements-optional.txt"
            ) from exc

        self._model = model
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        import anthropic

        messages = [*request.history, {"role": "user", "content": request.text}]
        kwargs: dict = {
            "model": self._model,
            "max_tokens": 1024,
            "messages": messages,
        }
        if request.system_context:
            kwargs["system"] = request.system_context

        try:
            response = await self._client.messages.create(**kwargs)
        except anthropic.APIConnectionError as exc:
            raise AssistantProviderError(f"Anthropic API connection failed: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise AssistantProviderError(
                f"Anthropic API error {exc.status_code}: {exc.message}"
            ) from exc

        if response.stop_reason == "refusal":
            raise AssistantProviderError("Anthropic API declined the request (refusal)")

        text_blocks = [block.text for block in response.content if block.type == "text"]
        text = "\n".join(text_blocks).strip()
        if not text:
            raise AssistantProviderError("Anthropic API returned no text content")
        return AssistantReply(text=text, provider=self.name)
