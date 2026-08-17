from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel

from app.deps import get_voice_providers
from app.voice_state import VoiceProviders
from assistant.router import AssistantRouter
from voice.audio_utils import wav_to_pcm16
from voice.errors import VoiceProviderError

logger = logging.getLogger("tars.voice_api")

router = APIRouter(tags=["voice"])

VOICE_READY_TIMEOUT_SECONDS = 5.0


class TranscribeResponse(BaseModel):
    text: str
    language: str | None = None


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
        "silero",
        "faster_whisper",
        "kokoro",
        "fish_speech",
        "mock",
    ]


@router.get("/api/v1/voice/status", response_model=VoiceStatusResponse)
@router.get("/api/voice/status", response_model=VoiceStatusResponse)
async def status(voice: VoiceProviders = Depends(get_voice_providers)) -> VoiceStatusResponse:
    return VoiceStatusResponse(
        ready=voice.ready.is_set(),
        wake_word_provider=voice.wake_word.name,
        stt_provider=voice.stt.name,
        tts_provider=voice.tts.name,
        vad_provider="silero",
        supported_providers=[
            "openwakeword",
            "silero",
            "faster_whisper",
            "kokoro",
            "fish_speech",
            "mock",
        ],
    )


@router.post("/api/v1/voice/transcribe", response_model=TranscribeResponse)
@router.post("/api/voice/transcribe", response_model=TranscribeResponse)
async def transcribe(
    file: UploadFile,
    voice: VoiceProviders = Depends(get_voice_providers),
) -> TranscribeResponse:
    wav_bytes = await file.read()
    try:
        pcm, _sample_rate = wav_to_pcm16(wav_bytes)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid WAV upload: {exc}") from exc

    try:
        result = await voice.stt.transcribe(pcm)
    except VoiceProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return TranscribeResponse(text=result.text, language=result.language)


@router.post("/api/v1/voice/synthesize")
@router.post("/api/voice/synthesize")
async def synthesize(
    body: SynthesizeRequest,
    voice: VoiceProviders = Depends(get_voice_providers),
) -> Response:
    try:
        result = await voice.tts.synthesize(body.text)
    except VoiceProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return Response(content=result.audio, media_type="audio/wav")


@router.websocket("/api/v1/voice/session")
@router.websocket("/api/voice/session")
async def voice_session(
    websocket: WebSocket,
    conversation_id: str | None = None,
) -> None:
    voice_providers: VoiceProviders = websocket.app.state.voice_providers
    try:
        await asyncio.wait_for(voice_providers.ready.wait(), timeout=VOICE_READY_TIMEOUT_SECONDS)
    except TimeoutError:
        await websocket.close(code=1013, reason="voice providers still loading")
        return

    await websocket.accept()

    db = websocket.app.state.db
    from assistant.conversation_store import ConversationStore
    from events.service import EventService

    assistant_router: AssistantRouter = AssistantRouter(
        event_service=EventService(db.conn),
        conversation_store=ConversationStore(db.conn),
        provider=websocket.app.state.assistant_provider,
        memory_service=websocket.app.state.memory_service,
    )

    conversation_id = conversation_id or str(uuid4())

    from voice.pipeline import run_voice_session

    try:
        await run_voice_session(
            websocket=websocket,
            stt_provider=voice_providers.stt,
            tts_provider=voice_providers.tts,
            assistant_router=assistant_router,
            conversation_id=conversation_id,
        )
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("voice session %s ended with an error", conversation_id)
