"""Bridges Pipecat's frame pipeline to TARS's own AssistantRouter, so a
realtime voice turn resolves through the exact same deterministic-vs-model
routing, grounding, and conversation persistence as a text turn (Phase B) —
there is no second, parallel "voice assistant" implementation.
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

from assistant.router import AssistantRouter

logger = logging.getLogger("tars.voice.bridge")


class AssistantBridgeProcessor(FrameProcessor):
    """On a `TranscriptionFrame` (STT output), runs the transcribed text
    through `AssistantRouter.handle_text` and emits the reply as a plain
    `TextFrame` — deliberately not a `TranscriptionFrame`, since
    TTSService explicitly excludes that subtype from synthesis (it exists
    to mark STT output, not something to speak)."""

    def __init__(self, assistant_router: AssistantRouter, conversation_id: str):
        super().__init__()
        self._router = assistant_router
        self._conversation_id = conversation_id

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            text = frame.text.strip()
            if not text:
                return
            await self.push_frame(LLMFullResponseStartFrame(), direction)
            try:
                reply = await self._router.handle_text(text, self._conversation_id)
                await self.push_frame(TextFrame(text=reply.assistant_message.content), direction)
            except Exception as exc:
                logger.exception("assistant bridge failed to produce a reply")
                await self.push_frame(ErrorFrame(error=str(exc)), direction)
            finally:
                await self.push_frame(LLMFullResponseEndFrame(), direction)
        else:
            await self.push_frame(frame, direction)
