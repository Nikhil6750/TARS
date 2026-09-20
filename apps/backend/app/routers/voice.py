from __future__ import annotations

import asyncio

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

from app.deps import get_turn_controller, get_voice_providers
from app.voice_state import VoiceProviders
from app.voice_telemetry import VoiceTurnRecorder
from assistant.response_quality import prepare_speech_text, public_error_message
from assistant.turn_controller import (
    AssistantResponse,
    AssistantTurnController,
    DuplicateTurnConflict,
)
from voice.audio_utils import wav_to_pcm16
from voice.errors import VoiceProviderError

router = APIRouter(tags=["voice"])

VOICE_READY_TIMEOUT_SECONDS = 5.0


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
        pcm, _sample_rate = wav_to_pcm16(wav_bytes)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail="The audio upload isn't a supported WAV file.",
        ) from exc

    header_turn_id = request.headers.get("X-TARS-Turn-ID")
    try:
        return await controller.execute_utterance(
            pcm,
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
        pcm, _sample_rate = wav_to_pcm16(wav_bytes)
    except Exception as exc:
        await telemetry.fail("audio_decode")
        raise HTTPException(
            status_code=422,
            detail="The audio upload isn't a supported WAV file.",
        ) from exc

    try:
        await telemetry.mark("stt_started")
        result = await voice.stt.transcribe(pcm)
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
