"""Durable, timestamped stage telemetry for real voice turns."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import aiosqlite

VOICE_STAGES = (
    "audio_received",
    "audio_detected",
    "speech_end",
    "stt_started",
    "stt_completed",
    "command_available",
    "wake_detected",
    "command_ready",
    "processing_started",
    "assistant_first_text",
    "first_response_token",
    "tts_synthesis_started",
    "tts_started",
    "tts_ready",
    "tts_completed",
)


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class VoiceTurnTrace:
    turn_id: str
    conversation_id: str | None
    created_at: str
    audio_received_at: str | None = None
    audio_detected_at: str | None = None
    speech_end_at: str | None = None
    stt_started_at: str | None = None
    stt_completed_at: str | None = None
    transcript: str | None = None
    wake_match: str | None = None
    wake_detected_at: str | None = None
    command_available_at: str | None = None
    command_ready_at: str | None = None
    processing_started_at: str | None = None
    assistant_first_text_at: str | None = None
    first_response_token_at: str | None = None
    tts_synthesis_started_at: str | None = None
    tts_started_at: str | None = None
    tts_ready_at: str | None = None
    tts_completed_at: str | None = None
    provider: str | None = None
    status: str = "in_progress"
    error_stage: str | None = None

    def mark(self, stage: str, timestamp: str | None = None) -> None:
        if stage not in VOICE_STAGES:
            raise ValueError(f"unknown voice telemetry stage: {stage}")
        field_name = f"{stage}_at"
        if getattr(self, field_name) is None:
            setattr(self, field_name, timestamp or utc_timestamp())
        if stage in ("tts_ready", "tts_completed"):
            self.status = "completed"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class VoiceTraceStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def record(self, trace: VoiceTurnTrace) -> None:
        await self._conn.execute(
            """
            INSERT INTO voice_turn_traces (
                turn_id, conversation_id, audio_received_at, stt_started_at,
                stt_completed_at, command_available_at, assistant_first_text_at,
                tts_synthesis_started_at, tts_ready_at, status, error_stage, created_at,
                audio_detected_at, speech_end_at, transcript, wake_match,
                wake_detected_at, command_ready_at, processing_started_at,
                first_response_token_at, tts_started_at, tts_completed_at, provider
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(turn_id) DO UPDATE SET
                audio_received_at=excluded.audio_received_at,
                stt_started_at=excluded.stt_started_at,
                stt_completed_at=excluded.stt_completed_at,
                command_available_at=excluded.command_available_at,
                assistant_first_text_at=excluded.assistant_first_text_at,
                tts_synthesis_started_at=excluded.tts_synthesis_started_at,
                tts_ready_at=excluded.tts_ready_at,
                audio_detected_at=excluded.audio_detected_at,
                speech_end_at=excluded.speech_end_at,
                transcript=excluded.transcript,
                wake_match=excluded.wake_match,
                wake_detected_at=excluded.wake_detected_at,
                command_ready_at=excluded.command_ready_at,
                processing_started_at=excluded.processing_started_at,
                first_response_token_at=excluded.first_response_token_at,
                tts_started_at=excluded.tts_started_at,
                tts_completed_at=excluded.tts_completed_at,
                provider=excluded.provider,
                status=excluded.status,
                error_stage=excluded.error_stage
            """,
            (
                trace.turn_id,
                trace.conversation_id,
                trace.audio_received_at,
                trace.stt_started_at,
                trace.stt_completed_at,
                trace.command_available_at,
                trace.assistant_first_text_at,
                trace.tts_synthesis_started_at,
                trace.tts_ready_at,
                trace.status,
                trace.error_stage,
                trace.created_at,
                trace.audio_detected_at,
                trace.speech_end_at,
                trace.transcript,
                trace.wake_match,
                trace.wake_detected_at,
                trace.command_ready_at,
                trace.processing_started_at,
                trace.first_response_token_at,
                trace.tts_started_at,
                trace.tts_completed_at,
                trace.provider,
            ),
        )
        await self._conn.commit()

    async def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT * FROM voice_turn_traces ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]


class VoiceTurnRecorder:
    """Coordinates the current sequential utterance across Pipecat stages."""

    def __init__(self, store: VoiceTraceStore, conversation_id: str | None) -> None:
        self._store = store
        self._conversation_id = conversation_id
        self._current: VoiceTurnTrace | None = None
        self._lock = asyncio.Lock()

    @property
    def turn_id(self) -> str | None:
        return self._current.turn_id if self._current is not None else None

    async def start_turn(
        self,
        *,
        turn_id: str | None = None,
        audio_received: bool = False,
        audio_detected_at: str | None = None,
        speech_end_at: str | None = None,
    ) -> VoiceTurnTrace:
        async with self._lock:
            self._current = VoiceTurnTrace(
                turn_id=turn_id or uuid4().hex,
                conversation_id=self._conversation_id,
                created_at=utc_timestamp(),
                audio_detected_at=audio_detected_at,
                speech_end_at=speech_end_at,
            )
            if audio_received:
                self._current.mark("audio_received")
            await self._store.record(self._current)
            return self._current

    async def annotate(
        self,
        *,
        transcript: str | None = None,
        wake_match: str | None = None,
        provider: str | None = None,
    ) -> None:
        async with self._lock:
            if self._current is None:
                raise RuntimeError("voice turn must be started before annotation")
            if transcript is not None:
                self._current.transcript = transcript
            if wake_match is not None:
                self._current.wake_match = wake_match
            if provider is not None:
                self._current.provider = provider
            await self._store.record(self._current)

    async def finish(self, status: str = "completed") -> None:
        async with self._lock:
            if self._current is None:
                raise RuntimeError("voice turn must be started before completion")
            self._current.status = status
            await self._store.record(self._current)

    async def mark(self, stage: str) -> str:
        async with self._lock:
            if self._current is None or (
                stage == "audio_received" and self._current.tts_ready_at is not None
            ):
                self._current = VoiceTurnTrace(
                    turn_id=uuid4().hex,
                    conversation_id=self._conversation_id,
                    created_at=utc_timestamp(),
                )
            self._current.mark(stage)
            await self._store.record(self._current)
            return self._current.turn_id

    async def fail(self, stage: str) -> None:
        async with self._lock:
            if self._current is None:
                self._current = VoiceTurnTrace(
                    turn_id=uuid4().hex,
                    conversation_id=self._conversation_id,
                    created_at=utc_timestamp(),
                )
            self._current.status = "failed"
            self._current.error_stage = stage
            await self._store.record(self._current)
