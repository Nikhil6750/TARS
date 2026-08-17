"""Bridges Pipecat's frame pipeline to TARS's own AssistantRouter, so a
realtime voice turn resolves through the exact same deterministic-vs-model
routing, grounding, and conversation persistence as a text turn (Phase B) —
there is no second, parallel "voice assistant" implementation.

Wave 2A: a transcript is first offered to `skills.voice_bridge` (the same
small, fixed deterministic-phrase parser the HUD's own bypass uses) before
falling through to the assistant/LLM path. A deterministic match still goes
through the full `ActionRuntime.submit()` -- permission classification,
skill registry dispatch, audit -- exactly like a HUD- or hotkey-issued
request; this only skips the *LLM interpretation* step, never the runtime.
"""
from __future__ import annotations

import logging

from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TextFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from actions.runtime import ActionRuntime
from app.action_contracts import ActionResult, ActionSource, ActionStatus
from assistant.router import AssistantRouter
from skills.voice_bridge import build_action_request_from_voice

logger = logging.getLogger("tars.voice.bridge")


class AssistantBridgeProcessor(FrameProcessor):
    """On a `TranscriptionFrame` (STT output): if the transcript matches a
    deterministic action phrase, submits it to the real `ActionRuntime` and
    speaks its truthful `ActionResult.summary`. Otherwise (no match, or no
    `action_runtime` wired in) falls through unchanged to
    `AssistantRouter.handle_text`. Emits the reply as a plain `TextFrame` —
    deliberately not a `TranscriptionFrame`, since TTSService explicitly
    excludes that subtype from synthesis (it exists to mark STT output, not
    something to speak)."""

    def __init__(
        self,
        assistant_router: AssistantRouter,
        conversation_id: str,
        *,
        action_runtime: ActionRuntime | None = None,
        action_source: ActionSource = ActionSource.voice_ptt,
    ):
        super().__init__()
        self._router = assistant_router
        self._conversation_id = conversation_id
        self._action_runtime = action_runtime
        self._action_source = action_source

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            text = frame.text.strip()
            if not text:
                return
            await self.push_frame(LLMFullResponseStartFrame(), direction)
            try:
                reply_text = await self._handle_transcript(text)
                await self.push_frame(TextFrame(text=reply_text), direction)
            except Exception as exc:
                logger.exception("assistant bridge failed to produce a reply")
                await self.push_frame(ErrorFrame(error=str(exc)), direction)
            finally:
                await self.push_frame(LLMFullResponseEndFrame(), direction)
        else:
            await self.push_frame(frame, direction)

    async def _handle_transcript(self, text: str) -> str:
        if self._action_runtime is not None:
            action_request = build_action_request_from_voice(text, source=self._action_source)
            if action_request is not None:
                result = await self._action_runtime.submit(action_request)
                return _speak_action_result(result)

        # Not a recognized deterministic action phrase (or no action runtime
        # wired in, e.g. some test harnesses) -- unchanged, existing path.
        reply = await self._router.handle_text(text, self._conversation_id)
        return reply.assistant_message.content


def _speak_action_result(result: ActionResult) -> str:
    """A truthful spoken rendering of a real ActionResult -- never invents
    outcome language beyond what the runtime/skill actually reported."""
    if result.error and result.status != ActionStatus.SUCCEEDED:
        return f"{result.summary} {result.error}".strip()
    return result.summary
