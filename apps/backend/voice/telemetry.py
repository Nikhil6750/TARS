"""Pipecat frame hook for the first backend-observed audio of an utterance."""
from __future__ import annotations

from pipecat.frames.frames import Frame, UserStartedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.voice_telemetry import VoiceTurnRecorder


class VoiceAudioTelemetryProcessor(FrameProcessor):
    def __init__(self, recorder: VoiceTurnRecorder) -> None:
        super().__init__()
        self._recorder = recorder

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, UserStartedSpeakingFrame):
            await self._recorder.mark("audio_received")
        await self.push_frame(frame, direction)
