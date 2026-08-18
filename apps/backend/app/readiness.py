"""Runtime readiness: the single source of truth for "is TARS actually
ready for real voice interaction, or is it quietly running on mocks".

Never crashes the app over this -- mock providers remain a legitimate,
fully-supported dev/test mode (see app/config.py's field defaults and
requirements-voice.txt). What this module refuses to do is let the app
*pretend* mock is real: `ready` is only true when every component that
matters for the voice-first experience is both configured to a real
provider and actually constructed successfully.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass

from app.config import Settings
from app.voice_state import VoiceProviders

REQUIRED_ASSISTANT_PROVIDER = "claude_code"
REQUIRED_STT_PROVIDER = "faster_whisper"
REQUIRED_TTS_PROVIDER = "kokoro"

NOT_CONFIGURED_MESSAGE = "TARS voice services are not configured."


@dataclass
class ComponentStatus:
    configured: str
    expected: str | None
    ready: bool
    detail: str | None = None


@dataclass
class ReadinessReport:
    assistant: ComponentStatus
    stt: ComponentStatus
    tts: ComponentStatus
    wake: ComponentStatus
    database: ComponentStatus
    claude_cli: ComponentStatus
    ready: bool
    message: str | None

    def to_dict(self) -> dict:
        def component_dict(c: ComponentStatus) -> dict:
            return {
                "configured": c.configured,
                "expected": c.expected,
                "ready": c.ready,
                "detail": c.detail,
            }

        return {
            "assistant": component_dict(self.assistant),
            "stt": component_dict(self.stt),
            "tts": component_dict(self.tts),
            "wake": component_dict(self.wake),
            "database": component_dict(self.database),
            "claude_cli": component_dict(self.claude_cli),
            "ready": self.ready,
            "message": self.message,
        }


def _claude_cli_status(settings: Settings) -> ComponentStatus:
    resolved = shutil.which(settings.claude_code_command)
    return ComponentStatus(
        configured=settings.claude_code_command,
        expected=None,
        ready=resolved is not None,
        detail=resolved or f"'{settings.claude_code_command}' not found on PATH",
    )


async def build_readiness_report(
    settings: Settings,
    voice: VoiceProviders,
    *,
    database_ok: bool,
) -> ReadinessReport:
    assistant_ready = settings.assistant_provider == REQUIRED_ASSISTANT_PROVIDER
    stt_ready = voice.ready.is_set() and voice.stt.name == REQUIRED_STT_PROVIDER
    tts_ready = voice.ready.is_set() and voice.tts.name == REQUIRED_TTS_PROVIDER
    claude_cli = _claude_cli_status(settings)

    assistant = ComponentStatus(
        configured=settings.assistant_provider,
        expected=REQUIRED_ASSISTANT_PROVIDER,
        ready=assistant_ready,
    )
    stt = ComponentStatus(
        configured=voice.stt.name if voice.ready.is_set() else settings.stt_provider,
        expected=REQUIRED_STT_PROVIDER,
        ready=stt_ready,
        detail=None if voice.ready.is_set() else "voice providers still loading",
    )
    tts = ComponentStatus(
        configured=voice.tts.name if voice.ready.is_set() else settings.tts_provider,
        expected=REQUIRED_TTS_PROVIDER,
        ready=tts_ready,
        detail=None if voice.ready.is_set() else "voice providers still loading",
    )
    # No trained "Hey TARS" wake-word model exists (see
    # requirements-voice.txt) -- wake detection runs as native background
    # VAD + this same STT transcription (see src-tauri/src/wake_engine.rs),
    # not a separate wake_word_provider, so its readiness tracks STT's.
    wake = ComponentStatus(
        configured="native_vad_whisper",
        expected="native_vad_whisper",
        ready=stt_ready,
        detail="wake detection runs in the native Tauri runtime and depends on STT readiness",
    )
    database = ComponentStatus(
        configured="sqlite",
        expected=None,
        ready=database_ok,
    )

    ready = (
        assistant.ready
        and stt.ready
        and tts.ready
        and wake.ready
        and database.ready
        and claude_cli.ready
    )

    return ReadinessReport(
        assistant=assistant,
        stt=stt,
        tts=tts,
        wake=wake,
        database=database,
        claude_cli=claude_cli,
        ready=ready,
        message=None if ready else NOT_CONFIGURED_MESSAGE,
    )
