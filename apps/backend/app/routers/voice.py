"""Voice/assistant API surface for clients (laptop Tauri shell, iPhone PWA,
later ESP32), per ARCHITECTURE.md § Voice orchestration and the Phase E
mission: text question, audio input, assistant response, audio output, and
conversation session/status. Never returns provider secrets — only
provider *names* (see VoiceStatusResponse) — to the browser.
"""
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

router = APIRouter(prefix="/api/v1/voice", tags=["voice"])

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


@router.get("/status", response_model=VoiceStatusResponse)
async def status(voice: VoiceProviders = Depends(get_voice_providers)) -> VoiceStatusResponse:
    return VoiceStatusResponse(
        ready=voice.ready.is_set(),
        wake_word_provider=voice.wake_word.name,
        stt_provider=voice.stt.name,
        tts_provider=voice.tts.name,
    )


@router.post("/transcribe", response_model=TranscribeResponse)
async def transcribe(
    file: UploadFile,
    voice: VoiceProviders = Depends(get_voice_providers),
) -> TranscribeResponse:
    """Accepts a WAV file (16-bit PCM, mono) and returns its transcript.
    Audio-format transcoding (e.g. browser webm/opus) is a client
    responsibility — this endpoint expects WAV specifically."""
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


@router.post("/synthesize")
async def synthesize(
    body: SynthesizeRequest,
    voice: VoiceProviders = Depends(get_voice_providers),
) -> Response:
    try:
        result = await voice.tts.synthesize(body.text)
    except VoiceProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return Response(content=result.audio, media_type="audio/wav")


@router.websocket("/session")
async def voice_session(
    websocket: WebSocket,
    conversation_id: str | None = None,
) -> None:
    """Realtime voice session: audio in -> VAD -> STT -> assistant ->
    TTS -> audio out, over one WebSocket connection, per client push-to-talk
    or continuous-listening UX (see ARCHITECTURE.md § Voice orchestration —
    push-to-talk is guaranteed regardless of wake-word provider state,
    because this endpoint never requires wake-word detection to begin)."""
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
