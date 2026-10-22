"""CONVERSATION path: ordinary questions answered by ONE fast model (never Claude + ChatGPT in sequence).

Candidates are tried in order; a transient failure (timeout/5xx/network/interrupted stream) before any text moves to the next candidate, and if
every candidate fails the user hears a truthful 'I can't reach my language model' line - never a made-up answer. Text already streamed is never
replayed or rewritten by a fallback (that would deliver two different answers)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator, Callable

from .openai_client import OpenAIAuthError, OpenAIError, OpenAIResponsesClient, OpenAIUnavailable

SYSTEM_PROMPT = (
    "You are TARS, a quiet, capable personal assistant speaking aloud. Answer in one or two short natural sentences, plain language, no markdown or lists. "
    "You have no clock, no market data and no access to the user's machine in this mode: if asked for the time, a price, today's date or anything live, "
    "say you can't see that from here instead of guessing. Never give trading signals or predictions."
)
TRUTHFUL_FAILURE = "I can't reach my language model right now, so I can't answer that. Local commands still work."
TRUTHFUL_AUTH = "My ChatGPT connection needs you to sign in again, so I can't answer that right now. Local commands still work."


@dataclass
class Candidate:
    name: str                                   # model id (or provider label)
    stream: Callable[[str, list], AsyncIterator[str]]


def openai_candidate(client: OpenAIResponsesClient, model: str, effort: str | None = "low") -> Candidate:
    async def stream(text: str, history: list) -> AsyncIterator[str]:
        async for kind, payload in client.stream(model, SYSTEM_PROMPT, text, max_output_tokens=None, reasoning_effort=effort, history=history):
            if kind == "delta":
                yield payload
    return Candidate(model, stream)


class ConversationService:
    def __init__(self, candidates: list[Candidate], *, first_token_timeout_s: float = 12.0, max_history: int = 6) -> None:
        self.candidates, self.first_token_timeout_s, self.max_history = candidates, first_token_timeout_s, max_history
        self.history: list[dict] = []
        self.last_model: str | None = None
        self.attempts: list[tuple[str, str]] = []

    async def stream(self, text: str) -> AsyncIterator[str]:
        self.attempts = []
        hist = self.history[-self.max_history:]
        answer = ""
        for cand in self.candidates:
            started = False
            try:
                it = cand.stream(text, hist).__aiter__()
                while True:
                    try:
                        chunk = await asyncio.wait_for(it.__anext__(), timeout=self.first_token_timeout_s if not started else 30.0)
                    except StopAsyncIteration:
                        break
                    started = True
                    answer += chunk
                    yield chunk
                if started:
                    self.last_model = cand.name
                    self.history += [{"role": "user", "content": text}, {"role": "assistant", "content": answer}]
                    self.attempts.append((cand.name, "ok"))
                    return
                self.attempts.append((cand.name, "empty"))
            except OpenAIAuthError:
                self.attempts.append((cand.name, "auth"))
                if not started:
                    yield TRUTHFUL_AUTH
                    return
                return
            except (OpenAIError, asyncio.TimeoutError, OSError) as exc:
                self.attempts.append((cand.name, type(exc).__name__))
                if started:       # partial answer already delivered: do not splice in a different model's words
                    return
        yield TRUTHFUL_FAILURE
