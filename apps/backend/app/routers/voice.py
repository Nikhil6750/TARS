from __future__ import annotations

import asyncio
import logging
import os

from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import Response
from pydantic import BaseModel

from app.config import REPO_ROOT
from app.deps import get_turn_controller, get_voice_providers
from app.voice_state import VoiceProviders
from app.voice_telemetry import VoiceTurnRecorder
from assistant.response_quality import prepare_speech_text, public_error_message
from assistant.turn_controller import (
    AssistantResponse,
    AssistantTurnController,
    DuplicateTurnConflict,
)
from voice.audio_utils import pcm16_stats, resample_pcm16, wav_channels, wav_to_pcm16
from voice.errors import VoiceProviderError

router = APIRouter(tags=["voice"])
debug_logger = logging.getLogger("tars.voice.debug_capture")

VOICE_READY_TIMEOUT_SECONDS = 5.0

# Temporary physical-audio root-cause diagnostics (TARS_VOICE_DEBUG_CAPTURE=1).
# Saves the exact WAV bytes this endpoint received, byte-for-byte, before any
# decoding/transcription touches them. Not committed -- see .gitignore.
_DEBUG_CAPTURE_DIR = REPO_ROOT / "artifacts" / "voice-debug"
_DEBUG_CAPTURE_KEEP = 10


def _maybe_capture_debug_audio(
    wav_bytes: bytes, *, turn_id: str | None, pcm: bytes, sample_rate: int
) -> None:
    if os.environ.get("TARS_VOICE_DEBUG_CAPTURE") != "1":
        return
    try:
        _DEBUG_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        safe_turn_id = turn_id or "unknown"
        target = _DEBUG_CAPTURE_DIR / f"{safe_turn_id}.wav"
        target.write_bytes(wav_bytes)
        (_DEBUG_CAPTURE_DIR / "latest.wav").write_bytes(wav_bytes)

        stats = pcm16_stats(pcm, sample_rate)
        channels = wav_channels(wav_bytes)
        debug_logger.info(
            "turn_id=%s duration_ms=%s sample_rate=%s channels=%s sample_count=%s "
            "peak_amplitude=%s rms=%s dc_offset=%s saved=%s",
            safe_turn_id,
            stats["duration_ms"],
            sample_rate,
            channels,
            stats["sample_count"],
            stats["peak_amplitude"],
            stats["rms"],
            stats["dc_offset"],
            target,
        )

        kept = sorted(
            (p for p in _DEBUG_CAPTURE_DIR.glob("*.wav") if p.name != "latest.wav"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in kept[_DEBUG_CAPTURE_KEEP:]:
            stale.unlink(missing_ok=True)
    except OSError:
        debug_logger.exception("voice debug capture failed for turn_id=%s", turn_id)


class TranscribeResponse(BaseModel):
    text: str
    language: str | None = None
    telemetry_id: str | None = None


class SynthesizeRequest(BaseModel):
    text: str


class VoiceStatusResponse(BaseModel):
    ready: bool
    wake_word_provider: str
    stt_provider: str
    tts_provider: str
    vad_provider: str = "silero"
    supported_providers: list[str] = [
        "openwakeword",
        "transcript_matcher",
        "silero",
        "faster_whisper",
        "kokoro",
        "fish_speech",
        "mock",
    ]


@router.get("/api/v1/voice/status", response_model=VoiceStatusResponse)
async def status(voice: VoiceProviders = Depends(get_voice_providers)) -> VoiceStatusResponse:
    return VoiceStatusResponse(
        ready=voice.ready.is_set(),
        wake_word_provider="transcript_matcher",
        stt_provider=voice.stt.name,
        tts_provider=voice.tts.name,
        vad_provider="silero",
        supported_providers=[
            "openwakeword",
            "transcript_matcher",
            "silero",
            "faster_whisper",
            "kokoro",
            "fish_speech",
            "mock",
        ],
    )


class GeminiLiveStatusResponse(BaseModel):
    enabled: bool
    state: str
    device_name: str | None = None
    error: str | None = None
    # Live runtime evidence for the mic -> Gemini -> transcript -> TARS ->
    # audio chain, so a stuck stage is diagnosable from real state instead
    # of guessed. See voice/gemini_live_loop.py's GeminiLiveStatus.
    connected: bool = False
    mic_streaming: bool = False
    last_audio_sent_at: str | None = None
    last_input_transcript: str | None = None
    last_turn_received_at: str | None = None
    last_tars_response: str | None = None
    last_audio_response_at: str | None = None
    last_error: str | None = None
    # The one authoritative manual-listening flag -- frontend must read
    # this, never guess/derive it locally. See GeminiLiveLoop.status.
    manual_listening_enabled: bool = False


@router.get("/api/v1/voice/gemini-status", response_model=GeminiLiveStatusResponse)
async def gemini_status(request: Request) -> GeminiLiveStatusResponse:
    """Polled by the native UI to show REAL Gemini Live state (starting/
    voice_off/listening/user_speaking/processing/speaking/stopped) instead
    of inventing its own -- when this loop isn't the active mic owner,
    `enabled=False` and the frontend falls back to observing the native
    Rust wake_engine path."""
    loop = getattr(request.app.state, "gemini_live_loop", None)
    if loop is None:
        return GeminiLiveStatusResponse(enabled=False, state="disabled")
    status = loop.status
    return GeminiLiveStatusResponse(
        enabled=True,
        state=status.state,
        device_name=status.device_name,
        error=status.error,
        connected=status.connected,
        mic_streaming=status.mic_streaming,
        last_audio_sent_at=status.last_audio_sent_at,
        last_input_transcript=status.last_input_transcript,
        last_turn_received_at=status.last_turn_received_at,
        last_tars_response=status.last_tars_response,
        last_audio_response_at=status.last_audio_response_at,
        last_error=status.last_error,
        manual_listening_enabled=status.manual_listening_enabled,
    )


class ManualListeningResponse(BaseModel):
    manual_listening_enabled: bool
    state: str


@router.post("/api/v1/voice/manual-listening/start", response_model=ManualListeningResponse)
async def start_manual_listening(request: Request) -> ManualListeningResponse:
    """Button press #1 (Start Listening) in the main Chat screen. Turns on
    mic forwarding to Gemini and accepts speech with no wake phrase
    required -- see GeminiLiveLoop.start_manual_listening(). 404s if
    Gemini Live isn't the active mic owner (GEMINI_LIVE_ENABLED=false or
    no API key) rather than silently no-op-ing, so the frontend can tell
    the difference between "off" and "not available here."""
    loop = getattr(request.app.state, "gemini_live_loop", None)
    if loop is None:
        raise HTTPException(status_code=404, detail="Gemini Live is not the active mic owner")
    await loop.start_manual_listening()
    return ManualListeningResponse(
        manual_listening_enabled=loop.status.manual_listening_enabled,
        state=loop.status.state,
    )


@router.post("/api/v1/voice/manual-listening/stop", response_model=ManualListeningResponse)
async def stop_manual_listening(request: Request) -> ManualListeningResponse:
    """Button press #2 (Stop Listening). Stops mic forwarding immediately,
    discards partial transcript/follow-up-window state, and flushes
    queued mic frames -- see GeminiLiveLoop.stop_manual_listening(). Does
    NOT disconnect Gemini Live or cancel a turn already in flight."""
    loop = getattr(request.app.state, "gemini_live_loop", None)
    if loop is None:
        raise HTTPException(status_code=404, detail="Gemini Live is not the active mic owner")
    await loop.stop_manual_listening()
    return ManualListeningResponse(
        manual_listening_enabled=loop.status.manual_listening_enabled,
        state=loop.status.state,
    )


@router.post("/api/v1/voice/utterance", response_model=AssistantResponse)
async def utterance(
    request: Request,
    file: UploadFile,
    conversation_id: str | None = Form(default=None),
    session_id: str = Form(default="native"),
    turn_id: str | None = Form(default=None),
    audio_detected_at_ms: int | None = Form(default=None),
    speech_end_at_ms: int | None = Form(default=None),
    controller: AssistantTurnController = Depends(get_turn_controller),
    voice: VoiceProviders = Depends(get_voice_providers),
) -> AssistantResponse:
    """Canonical desktop voice entry point: one VAD-complete WAV segment in,
    one backend-owned turn response out.  Native and React clients must not
    run wake matching, routing, provider execution, or speech composition
    after calling this endpoint."""

    try:
        await asyncio.wait_for(voice.ready.wait(), timeout=VOICE_READY_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail="Voice providers are still loading.") from exc

    wav_bytes = await file.read()
    try:
        pcm, sample_rate = wav_to_pcm16(wav_bytes)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail="The audio upload isn't a supported WAV file.",
        ) from exc

    header_turn_id = request.headers.get("X-TARS-Turn-ID")
    _maybe_capture_debug_audio(
        wav_bytes, turn_id=turn_id or header_turn_id, pcm=pcm, sample_rate=sample_rate
    )
    # Bridge captured-audio rate (whatever the OS/device negotiated, e.g.
    # 48000 Hz) to the STT provider's declared rate. Per voice/interfaces.py,
    # providers never resample internally -- the caller must.
    stt_pcm = resample_pcm16(pcm, sample_rate, voice.stt.sample_rate)
    try:
        return await controller.execute_utterance(
            stt_pcm,
            conversation_id=conversation_id,
            session_id=session_id,
            turn_id=turn_id or header_turn_id,
            audio_detected_at_ms=audio_detected_at_ms,
            speech_end_at_ms=speech_end_at_ms,
        )
    except DuplicateTurnConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/v1/voice/transcribe", response_model=TranscribeResponse, deprecated=True)
async def transcribe(
    request: Request,
    file: UploadFile,
    voice: VoiceProviders = Depends(get_voice_providers),
) -> TranscribeResponse:
    wav_bytes = await file.read()
    telemetry = VoiceTurnRecorder(request.app.state.voice_trace_store, None)
    await telemetry.start_turn(audio_received=True)
    try:
        pcm, sample_rate = wav_to_pcm16(wav_bytes)
    except Exception as exc:
        await telemetry.fail("audio_decode")
        raise HTTPException(
            status_code=422,
            detail="The audio upload isn't a supported WAV file.",
        ) from exc

    try:
        await telemetry.mark("stt_started")
        result = await voice.stt.transcribe(resample_pcm16(pcm, sample_rate, voice.stt.sample_rate))
    except VoiceProviderError as exc:
        await telemetry.fail("stt")
        raise HTTPException(status_code=503, detail=public_error_message("stt")) from exc

    await telemetry.mark("stt_completed")
    if result.text:
        await telemetry.mark("command_available")

    return TranscribeResponse(
        text=result.text,
        language=result.language,
        telemetry_id=telemetry.turn_id,
    )


@router.post("/api/v1/voice/synthesize", deprecated=True)
async def synthesize(
    request: Request,
    body: SynthesizeRequest,
    voice: VoiceProviders = Depends(get_voice_providers),
) -> Response:
    telemetry = VoiceTurnRecorder(request.app.state.voice_trace_store, None)
    await telemetry.start_turn()
    await telemetry.mark("tts_synthesis_started")
    try:
        result = await voice.tts.synthesize(prepare_speech_text(body.text))
    except VoiceProviderError as exc:
        await telemetry.fail("tts")
        raise HTTPException(status_code=503, detail=public_error_message("tts")) from exc

    await telemetry.mark("tts_ready")
    return Response(
        content=result.audio,
        media_type="audio/wav",
        headers={"X-TARS-Voice-Turn-ID": telemetry.turn_id or ""},
    )
