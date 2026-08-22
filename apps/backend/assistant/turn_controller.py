"""The single owner of a TARS human-assistant turn.

The controller is intentionally small at the top level.  It assigns one turn
ID, rejects/replays duplicates, chooses one explicit intent, executes exactly
one downstream path, composes one display/speech response, and publishes state
events for clients to observe.  Existing advanced runtimes remain downstream
dependencies; none may independently receive a second copy of the same turn.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from actions.runtime import ActionRuntime
from app.action_contracts import ActionResult, ActionSource, ActionStatus
from app.config import Settings
from app.latency_store import LatencyTraceStore, RequestTrace
from app.schemas import AssistantMessage, InputMode, MessageProviders, MessageRole
from app.voice_telemetry import VoiceTraceStore, VoiceTurnRecorder
from assistant.conversation_store import ConversationStore
from assistant.errors import AssistantProviderError
from assistant.hot_chart_state import Freshness
from assistant.hot_chart_state_store import HotChartStateStore
from assistant.provider import AssistantProvider, AssistantRequest
from assistant.response_quality import QUALITY_SYSTEM_PROMPT, ResponseComposer, public_error_message
from assistant.router import AssistantRouter, RouterReply
from orchestrator.orchestrator import TarsOrchestrator
from skills.voice_bridge import build_action_request_from_voice
from voice.errors import VoiceProviderError

if TYPE_CHECKING:
    from app.voice_state import VoiceProviders
    from memory.service import MemoryService

logger = logging.getLogger("tars.turn_controller")


class TurnIntent(str, Enum):
    DETERMINISTIC = "DETERMINISTIC"
    NORMAL_CONVERSATION = "NORMAL_CONVERSATION"
    CHART_ANALYSIS = "CHART_ANALYSIS"
    TOOL_TASK = "TOOL_TASK"
    RESEARCH = "RESEARCH"
    TRADING_RESEARCH = "TRADING_RESEARCH"


class TurnState(str, Enum):
    IDLE = "IDLE"
    SPEECH_DETECTED = "SPEECH_DETECTED"
    TRANSCRIBING = "TRANSCRIBING"
    WAKE_DETECTED = "WAKE_DETECTED"
    LISTENING_FOR_COMMAND = "LISTENING_FOR_COMMAND"
    PROCESSING = "PROCESSING"
    SPEAKING = "SPEAKING"


class TurnStatus(str, Enum):
    COMPLETED = "completed"
    AWAITING_COMMAND = "awaiting_command"
    IGNORED = "ignored"
    FAILED = "failed"


class AssistantResponse(BaseModel):
    """One final public response for one executed turn."""

    turn_id: str
    display_text: str
    speech_text: str
    intent: TurnIntent
    status: TurnStatus
    provider: str
    latency_ms: float = Field(ge=0)
    conversation_id: str
    transcript: str | None = None
    replayed: bool = False
    audio_chunks_base64: list[str] = Field(default_factory=list)


class TurnEvent(BaseModel):
    turn_id: str
    type: str
    state: TurnState | None = None
    text: str | None = None
    response: AssistantResponse | None = None
    timestamp_ms: int = Field(default_factory=lambda: round(time.time() * 1000))


class DuplicateTurnConflict(ValueError):
    """The caller reused a turn ID for different human input."""


@dataclass(frozen=True)
class WakeMatch:
    alias: str
    normalized_transcript: str
    command: str


class WakePhraseMatcher:
    """Configurable normalized transcript matcher; no wake ML is implied."""

    def __init__(self, aliases: list[str]) -> None:
        normalized = [normalize_transcript(alias) for alias in aliases]
        self._aliases = tuple(
            sorted({alias for alias in normalized if alias}, key=lambda item: (-len(item), item))
        )

    def match(self, transcript: str) -> WakeMatch | None:
        normalized = normalize_transcript(transcript)
        for alias in self._aliases:
            match = re.search(rf"(?:^|\s){re.escape(alias)}(?:\s|$)", normalized)
            if match is None:
                continue
            return WakeMatch(
                alias=alias,
                normalized_transcript=normalized,
                command=normalized[match.end() :].strip(),
            )
        return None


@dataclass
class _PendingCommand:
    conversation_id: str
    expires_at: float
    recorder: VoiceTurnRecorder
    timeout_task: asyncio.Task[None] | None = None


_CHART = re.compile(
    r"\b(?:analy[sz]e|check|read|review|inspect|scan|look\s+at)\b.*\bcharts?\b|"
    r"\bwhat\s+do\s+you\s+see\b.*\bcharts?\b",
    re.IGNORECASE,
)
_TRADING_RESEARCH = re.compile(
    r"\b(?:backtest|walk[- ]forward|deflated\s+sharpe|\bdsr\b|strategy\s+research|"
    r"trading\s+research|validated\s+(?:trade|strategy)|quant_brain)\b",
    re.IGNORECASE,
)
_RESEARCH = re.compile(
    r"\b(?:research|look\s+up|find\s+current|latest|today'?s|current\s+(?:ecb|fed|news)|"
    r"with\s+sources|cite\s+sources)\b",
    re.IGNORECASE,
)
_TOOL = re.compile(
    r"^\s*(?:create|write|edit|rename|move|delete|send|install|uninstall|use\s+skill|"
    r"run\s+command|execute|terminal)\b",
    re.IGNORECASE,
)
_ADVANCED_COMMAND = re.compile(
    r"\b(?:remember\s+that|what(?:'s|\s+is)\s+my\s+trading\s+context|"
    r"explain\s+the\s+setup|save\s+this\s+trading\s+observation|"
    r"search\s+trading\s+memory|set\s+up\s+my\s+trading\s+workspace|"
    r"run\s+(?:the\s+)?[a-z0-9_-]+\s+agent|"
    r"(?:install|uninstall|update|search|use|list).*\bskills?\b)\b",
    re.IGNORECASE,
)
_ACTION = re.compile(
    r"^\s*(?:open|launch|start|focus|search\s+(?:the\s+web\s+)?for)\b",
    re.IGNORECASE,
)
_TIME = re.compile(
    r"^\s*(?:what(?:'s|\s+is)\s+the\s+time|what\s+time\s+is\s+it|tell\s+me\s+the\s+time)\??\s*$",
    re.IGNORECASE,
)
_DATE = re.compile(
    r"^\s*(?:what(?:'s|\s+is)\s+(?:the\s+)?date|what\s+day\s+is\s+it|tell\s+me\s+the\s+date)\??\s*$",
    re.IGNORECASE,
)
_DETERMINISTIC_STATE = re.compile(
    r"\b(?:active\s+setups?|(?:needs?|requires?)\s+(?:my\s+)?attention|last\s+invalidation|"
    r"why\s+(?:was\s+)?(?:the\s+)?(?:last\s+)?setup\s+invalidated|"
    r"position\s+size|risk\s+reward|\br:r\b|should\s+i\s+enter)\b",
    re.IGNORECASE,
)


class TurnIntentRouter:
    """Small deterministic top-level router; it never calls a model."""

    def classify(self, text: str) -> TurnIntent:
        value = text.strip()
        if _CHART.search(value):
            return TurnIntent.CHART_ANALYSIS
        if _TRADING_RESEARCH.search(value):
            return TurnIntent.TRADING_RESEARCH
        if _RESEARCH.search(value):
            return TurnIntent.RESEARCH
        if _TIME.match(value) or _DATE.match(value) or _ACTION.match(value):
            return TurnIntent.DETERMINISTIC
        if _DETERMINISTIC_STATE.search(value):
            return TurnIntent.DETERMINISTIC
        if _TOOL.match(value) or _ADVANCED_COMMAND.search(value):
            return TurnIntent.TOOL_TASK
        return TurnIntent.NORMAL_CONVERSATION


class AssistantTurnController:
    """Canonical owner for text and recognized voice-command execution."""

    def __init__(
        self,
        *,
        settings: Settings,
        provider: AssistantProvider,
        assistant_router: AssistantRouter,
        orchestrator: TarsOrchestrator,
        action_runtime: ActionRuntime,
        conversation_store: ConversationStore,
        hot_chart_state_store: HotChartStateStore,
        trace_store: LatencyTraceStore | None = None,
        memory_service: MemoryService | None = None,
        voice_providers: VoiceProviders | None = None,
        voice_trace_store: VoiceTraceStore | None = None,
        cache_size: int = 256,
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._assistant_router = assistant_router
        self._orchestrator = orchestrator
        self._actions = action_runtime
        self._conversations = conversation_store
        self._hot_charts = hot_chart_state_store
        self._trace_store = trace_store
        self._memory = memory_service
        self._voice = voice_providers
        self._voice_trace_store = voice_trace_store
        self._router = TurnIntentRouter()
        self._wake_matcher = WakePhraseMatcher(settings.wake_alias_list)
        self._composer = ResponseComposer()
        self._cache_size = max(16, cache_size)
        self._lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Task[AssistantResponse]] = {}
        self._fingerprints: dict[str, str] = {}
        self._completed: OrderedDict[str, AssistantResponse] = OrderedDict()
        self._subscribers: dict[str, set[asyncio.Queue[TurnEvent]]] = {}
        self._execution_counts: dict[str, int] = {}
        self._utterance_inflight: dict[str, asyncio.Task[AssistantResponse]] = {}
        self._utterance_fingerprints: dict[str, str] = {}
        self._pending_commands: dict[str, _PendingCommand] = {}
        self._voice_recorders: dict[str, VoiceTurnRecorder] = {}
        self._first_token_marked: set[str] = set()

    def execution_count(self, turn_id: str) -> int:
        """Test/diagnostic evidence for the exactly-once invariant."""

        return self._execution_counts.get(turn_id, 0)

    async def execute_text(
        self,
        text: str,
        *,
        conversation_id: str | None = None,
        turn_id: str | None = None,
        input_mode: InputMode = InputMode.text,
        speak: bool = False,
    ) -> AssistantResponse:
        command = text.strip()
        if not command:
            raise ValueError("turn text must be non-empty")
        actual_turn_id = turn_id or uuid4().hex
        actual_conversation_id = str(_conversation_uuid(conversation_id or str(uuid4())))
        fingerprint = _fingerprint(command, actual_conversation_id, input_mode, speak)

        async with self._lock:
            known = self._fingerprints.get(actual_turn_id)
            if known is not None and known != fingerprint:
                raise DuplicateTurnConflict(
                    f"turn_id '{actual_turn_id}' was already assigned to different input"
                )
            cached = self._completed.get(actual_turn_id)
            if cached is not None:
                self._completed.move_to_end(actual_turn_id)
                return cached.model_copy(update={"replayed": True})
            task = self._inflight.get(actual_turn_id)
            if task is None:
                self._fingerprints[actual_turn_id] = fingerprint
                task = asyncio.create_task(
                    self._run_and_cache(
                        command,
                        turn_id=actual_turn_id,
                        conversation_id=actual_conversation_id,
                        input_mode=input_mode,
                        speak=speak,
                    )
                )
                self._inflight[actual_turn_id] = task
        return await asyncio.shield(task)

    async def stream_text(
        self,
        text: str,
        *,
        conversation_id: str | None = None,
        turn_id: str | None = None,
        input_mode: InputMode = InputMode.text,
        speak: bool = False,
    ) -> AsyncIterator[TurnEvent]:
        actual_turn_id = turn_id or uuid4().hex
        queue: asyncio.Queue[TurnEvent] = asyncio.Queue()
        async with self._lock:
            cached = self._completed.get(actual_turn_id)
            if cached is not None:
                yield TurnEvent(
                    turn_id=actual_turn_id,
                    type="complete",
                    response=cached.model_copy(update={"replayed": True}),
                )
                return
            self._subscribers.setdefault(actual_turn_id, set()).add(queue)

        task = asyncio.create_task(
            self.execute_text(
                text,
                conversation_id=conversation_id,
                turn_id=actual_turn_id,
                input_mode=input_mode,
                speak=speak,
            )
        )
        try:
            while True:
                event = await queue.get()
                yield event
                if event.type == "complete":
                    break
            await task
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(actual_turn_id)
                if subscribers is not None:
                    subscribers.discard(queue)
                    if not subscribers:
                        self._subscribers.pop(actual_turn_id, None)

    async def execute_utterance(
        self,
        pcm_audio: bytes,
        *,
        conversation_id: str | None = None,
        session_id: str = "native",
        turn_id: str | None = None,
        audio_detected_at_ms: int | None = None,
        speech_end_at_ms: int | None = None,
    ) -> AssistantResponse:
        """Transcribe and execute one native VAD-complete audio segment.

        The whole upload is idempotent, not only its downstream command: a
        network retry with the same turn ID cannot trigger a second STT call.
        """

        if self._voice is None or self._voice_trace_store is None:
            raise RuntimeError("voice providers and telemetry store are not configured")
        if not pcm_audio:
            raise ValueError("utterance audio must be non-empty")

        actual_turn_id = turn_id or uuid4().hex
        actual_conversation_id = str(_conversation_uuid(conversation_id or str(uuid4())))
        fingerprint = hashlib.sha256(
            session_id.encode("utf-8")
            + b"\0"
            + actual_conversation_id.encode("utf-8")
            + b"\0"
            + pcm_audio
        ).hexdigest()
        async with self._lock:
            known = self._utterance_fingerprints.get(actual_turn_id)
            if known is not None and known != fingerprint:
                raise DuplicateTurnConflict(
                    f"turn_id '{actual_turn_id}' was already assigned to different audio"
                )
            cached = self._completed.get(actual_turn_id)
            if cached is not None:
                return cached.model_copy(update={"replayed": True})
            task = self._utterance_inflight.get(actual_turn_id)
            if task is None:
                self._utterance_fingerprints[actual_turn_id] = fingerprint
                task = asyncio.create_task(
                    self._run_utterance(
                        pcm_audio,
                        turn_id=actual_turn_id,
                        conversation_id=actual_conversation_id,
                        session_id=session_id,
                        audio_detected_at_ms=audio_detected_at_ms,
                        speech_end_at_ms=speech_end_at_ms,
                    )
                )
                self._utterance_inflight[actual_turn_id] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                async with self._lock:
                    self._utterance_inflight.pop(actual_turn_id, None)

    async def _run_utterance(
        self,
        pcm_audio: bytes,
        *,
        turn_id: str,
        conversation_id: str,
        session_id: str,
        audio_detected_at_ms: int | None,
        speech_end_at_ms: int | None,
    ) -> AssistantResponse:
        assert self._voice is not None
        assert self._voice_trace_store is not None
        started = time.monotonic()
        pending = await self._take_pending_command(session_id)
        if pending is not None:
            conversation_id = pending.conversation_id
        recorder = VoiceTurnRecorder(self._voice_trace_store, conversation_id)
        await recorder.start_turn(
            turn_id=turn_id,
            audio_received=True,
            audio_detected_at=_iso_from_epoch_ms(audio_detected_at_ms),
            speech_end_at=_iso_from_epoch_ms(speech_end_at_ms),
        )
        if audio_detected_at_ms is None:
            await recorder.mark("audio_detected")
        if speech_end_at_ms is None:
            await recorder.mark("speech_end")
        await self._publish(
            TurnEvent(turn_id=turn_id, type="state", state=TurnState.SPEECH_DETECTED)
        )
        await recorder.mark("stt_started")
        await self._publish(
            TurnEvent(turn_id=turn_id, type="state", state=TurnState.TRANSCRIBING)
        )
        try:
            transcription = await self._voice.stt.transcribe(pcm_audio)
        except VoiceProviderError:
            await recorder.fail("stt")
            return await self._complete_without_execution(
                AssistantResponse(
                    turn_id=turn_id,
                    display_text=public_error_message("stt"),
                    speech_text=public_error_message("stt"),
                    intent=TurnIntent.DETERMINISTIC,
                    status=TurnStatus.FAILED,
                    provider=self._voice.stt.name,
                    latency_ms=round((time.monotonic() - started) * 1000, 2),
                    conversation_id=conversation_id,
                )
            )
        await recorder.mark("stt_completed")
        transcript = transcription.text.strip()
        await recorder.annotate(transcript=transcript)
        match = None if pending is not None else self._wake_matcher.match(transcript)
        if pending is not None:
            wake_match = "two_stage_command"
            command = normalize_transcript(transcript)
        elif match is not None:
            wake_match = match.alias
            command = match.command
        else:
            wake_match = None
            command = ""

        if wake_match is None:
            await recorder.fail("wake_match")
            return await self._complete_without_execution(
                AssistantResponse(
                    turn_id=turn_id,
                    display_text="",
                    speech_text="",
                    intent=TurnIntent.DETERMINISTIC,
                    status=TurnStatus.IGNORED,
                    provider=self._voice.stt.name,
                    latency_ms=round((time.monotonic() - started) * 1000, 2),
                    conversation_id=conversation_id,
                    transcript=transcript,
                )
            )

        await recorder.annotate(wake_match=wake_match)
        if pending is None:
            await recorder.mark("wake_detected")
            await self._publish(
                TurnEvent(turn_id=turn_id, type="state", state=TurnState.WAKE_DETECTED)
            )
        elif not command:
            await recorder.fail("stt")
            return await self._complete_without_execution(
                AssistantResponse(
                    turn_id=turn_id,
                    display_text=public_error_message("stt"),
                    speech_text=public_error_message("stt"),
                    intent=TurnIntent.DETERMINISTIC,
                    status=TurnStatus.FAILED,
                    provider=self._voice.stt.name,
                    latency_ms=round((time.monotonic() - started) * 1000, 2),
                    conversation_id=conversation_id,
                    transcript=transcript,
                )
            )
        if not command:
            acknowledgement = self._settings.wake_acknowledgement.strip() or "Yeah?"
            response = AssistantResponse(
                turn_id=turn_id,
                display_text=acknowledgement,
                speech_text=acknowledgement,
                intent=TurnIntent.DETERMINISTIC,
                status=TurnStatus.AWAITING_COMMAND,
                provider="wake_matcher",
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                conversation_id=conversation_id,
                transcript=transcript,
            )
            response = await self._synthesize_response(response, recorder)
            if response.status is TurnStatus.FAILED:
                return await self._complete_without_execution(response)
            await recorder.finish("awaiting_command")
            await self._set_pending_command(
                session_id,
                conversation_id=conversation_id,
                recorder=recorder,
                turn_id=turn_id,
            )
            await self._publish(
                TurnEvent(
                    turn_id=turn_id,
                    type="state",
                    state=TurnState.LISTENING_FOR_COMMAND,
                )
            )
            return await self._complete_without_execution(response)

        await recorder.mark("command_ready")
        await recorder.mark("processing_started")
        self._voice_recorders[turn_id] = recorder
        try:
            response = await self.execute_text(
                command,
                conversation_id=conversation_id,
                turn_id=turn_id,
                input_mode=InputMode.voice,
                speak=True,
            )
        finally:
            self._voice_recorders.pop(turn_id, None)
            self._first_token_marked.discard(turn_id)
        response = response.model_copy(
            update={
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
                "transcript": transcript,
            }
        )
        await recorder.annotate(provider=response.provider)
        if response.status is TurnStatus.FAILED and recorder.error_stage is None:
            await recorder.fail("provider")
        elif response.status is TurnStatus.COMPLETED:
            await recorder.finish("completed")
        async with self._lock:
            self._completed[turn_id] = response
        return response

    async def _synthesize_response(
        self, response: AssistantResponse, recorder: VoiceTurnRecorder
    ) -> AssistantResponse:
        if self._voice is None or not response.speech_text.strip():
            return response
        await self._publish(
            TurnEvent(turn_id=response.turn_id, type="state", state=TurnState.SPEAKING)
        )
        await recorder.mark("tts_started")
        audio_chunks: list[str] = []
        try:
            for chunk in sentence_chunks(response.speech_text):
                result = await self._voice.tts.synthesize(chunk)
                audio_chunks.append(base64.b64encode(result.audio).decode("ascii"))
        except VoiceProviderError:
            await recorder.fail("tts")
            return response.model_copy(update={"status": TurnStatus.FAILED})
        await recorder.mark("tts_completed")
        return response.model_copy(update={"audio_chunks_base64": audio_chunks})

    async def _take_pending_command(self, session_id: str) -> _PendingCommand | None:
        async with self._lock:
            pending = self._pending_commands.pop(session_id, None)
        if pending is None:
            return None
        if pending.timeout_task is not None:
            pending.timeout_task.cancel()
        if time.monotonic() > pending.expires_at:
            await pending.recorder.fail("command_timeout")
            return None
        return pending

    async def _set_pending_command(
        self,
        session_id: str,
        *,
        conversation_id: str,
        recorder: VoiceTurnRecorder,
        turn_id: str,
    ) -> None:
        timeout_seconds = min(8.0, max(0.01, self._settings.wake_command_timeout_seconds))
        pending = _PendingCommand(
            conversation_id=conversation_id,
            expires_at=time.monotonic() + timeout_seconds,
            recorder=recorder,
        )

        async def expire() -> None:
            try:
                await asyncio.sleep(timeout_seconds)
                async with self._lock:
                    if self._pending_commands.get(session_id) is not pending:
                        return
                    self._pending_commands.pop(session_id, None)
                await recorder.fail("command_timeout")
                await self._publish(
                    TurnEvent(turn_id=turn_id, type="state", state=TurnState.IDLE)
                )
            except asyncio.CancelledError:
                return

        pending.timeout_task = asyncio.create_task(expire())
        async with self._lock:
            previous = self._pending_commands.get(session_id)
            self._pending_commands[session_id] = pending
        if previous is not None and previous.timeout_task is not None:
            previous.timeout_task.cancel()

    async def _complete_without_execution(
        self, response: AssistantResponse
    ) -> AssistantResponse:
        async with self._lock:
            self._completed[response.turn_id] = response
            self._completed.move_to_end(response.turn_id)
        await self._publish(
            TurnEvent(turn_id=response.turn_id, type="complete", response=response)
        )
        return response

    async def _run_and_cache(
        self,
        text: str,
        *,
        turn_id: str,
        conversation_id: str,
        input_mode: InputMode,
        speak: bool,
    ) -> AssistantResponse:
        started = time.monotonic()
        self._execution_counts[turn_id] = self._execution_counts.get(turn_id, 0) + 1
        intent = self._router.classify(text)
        await self._publish(
            TurnEvent(turn_id=turn_id, type="state", state=TurnState.PROCESSING)
        )
        try:
            if intent is TurnIntent.NORMAL_CONVERSATION:
                display, provider = await self._normal_conversation(
                    text, turn_id=turn_id, conversation_id=conversation_id, input_mode=input_mode
                )
                presentation = self._composer.compose(user_text=text, display_text=display)
                await self._persist_message(
                    AssistantMessage(
                        conversation_id=_conversation_uuid(conversation_id),
                        role=MessageRole.assistant,
                        content=presentation.display_text,
                        input_mode=input_mode,
                        intent=TurnIntent.NORMAL_CONVERSATION.value,
                        providers=MessageProviders(assistant=provider),
                    )
                )
            elif intent is TurnIntent.CHART_ANALYSIS:
                display, provider = await self._chart_analysis(turn_id=turn_id)
                presentation = self._composer.compose(user_text=text, display_text=display)
            elif intent is TurnIntent.DETERMINISTIC:
                reply = await self._deterministic(text, conversation_id, input_mode)
                provider = (
                    reply.assistant_message.providers.assistant or "deterministic"
                )
                presentation = reply.presentation
            else:
                display, provider = await self._advanced(
                    text, turn_id=turn_id, conversation_id=conversation_id
                )
                presentation = self._composer.compose(user_text=text, display_text=display)

            response = AssistantResponse(
                turn_id=turn_id,
                display_text=presentation.display_text,
                speech_text=presentation.speech_text,
                intent=intent,
                status=TurnStatus.COMPLETED,
                provider=provider,
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                conversation_id=conversation_id,
            )
        except Exception:
            logger.exception("turn %s failed during %s", turn_id, intent.value)
            recorder = self._voice_recorders.get(turn_id)
            if recorder is not None:
                await recorder.fail("provider")
            response = AssistantResponse(
                turn_id=turn_id,
                display_text=public_error_message(),
                speech_text=public_error_message(),
                intent=intent,
                status=TurnStatus.FAILED,
                provider=getattr(self._provider, "name", "unknown"),
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                conversation_id=conversation_id,
            )

        if speak and response.status is TurnStatus.COMPLETED:
            recorder = self._voice_recorders.get(turn_id)
            if recorder is None:
                raise RuntimeError("voice turn lost its telemetry recorder before TTS")
            response = await self._synthesize_response(response, recorder)
        async with self._lock:
            self._completed[turn_id] = response
            self._completed.move_to_end(turn_id)
            while len(self._completed) > self._cache_size:
                old_turn_id, _ = self._completed.popitem(last=False)
                self._fingerprints.pop(old_turn_id, None)
            self._inflight.pop(turn_id, None)
        await self._publish(TurnEvent(turn_id=turn_id, type="complete", response=response))
        return response

    async def _normal_conversation(
        self,
        text: str,
        *,
        turn_id: str,
        conversation_id: str,
        input_mode: InputMode,
    ) -> tuple[str, str]:
        """Fast path: history + one provider, no tools/research/chart/memory."""

        user_message = AssistantMessage(
            conversation_id=_conversation_uuid(conversation_id),
            role=MessageRole.user,
            content=text,
            input_mode=input_mode,
        )
        await self._persist_message(user_message)
        history_rows = await self._conversations.get_recent(conversation_id, limit=10)
        history = [
            {"role": row["role"], "content": row["content"]}
            for row in history_rows
            if row["role"] in ("user", "assistant")
        ]
        request = AssistantRequest(
            text=text,
            conversation_id=conversation_id,
            system_context=(
                "This is the normal-conversation fast path. Answer directly without tools, "
                "research, chart analysis, or invented trading facts.\n\n" + QUALITY_SYSTEM_PROMPT
            ),
            history=history,
        )
        accumulated = ""
        provider_name = self._provider.name
        provider_started = time.monotonic()
        trace_started_at = datetime.now(UTC).isoformat()
        stream = getattr(self._provider, "respond_stream", None)
        try:
            if callable(stream):
                async for event in stream(request):
                    if event.get("type") == "delta":
                        chunk = str(event.get("text") or "")
                        if chunk:
                            accumulated += chunk
                            await self._publish(
                                TurnEvent(turn_id=turn_id, type="delta", text=chunk)
                            )
                    elif event.get("type") == "complete":
                        accumulated = str(event.get("text") or accumulated)
                        provider_name = str(event.get("provider") or provider_name)
            else:
                reply = await self._provider.respond(request)
                accumulated = reply.text
                provider_name = reply.provider
                await self._publish(
                    TurnEvent(turn_id=turn_id, type="delta", text=accumulated)
                )
        except AssistantProviderError as exc:
            await self._record_normal_trace(
                turn_id=turn_id,
                conversation_id=conversation_id,
                started_at=trace_started_at,
                started=provider_started,
                provider=provider_name,
                error=type(exc).__name__,
            )
            raise
        if not accumulated.strip():
            raise AssistantProviderError("provider returned an empty normal-conversation response")

        await self._record_normal_trace(
            turn_id=turn_id,
            conversation_id=conversation_id,
            started_at=trace_started_at,
            started=provider_started,
            provider=provider_name,
        )
        return accumulated, provider_name

    async def _persist_message(self, message: AssistantMessage) -> None:
        """Persist synchronously, then let memory observe without gating the turn."""

        await self._conversations.save(message)
        if self._memory is None:
            return
        task = asyncio.create_task(self._memory.index_conversation_message(message))
        task.add_done_callback(_log_memory_observer_failure)

    async def _record_normal_trace(
        self,
        *,
        turn_id: str,
        conversation_id: str,
        started_at: str,
        started: float,
        provider: str,
        error: str | None = None,
    ) -> None:
        if self._trace_store is None:
            return
        await self._trace_store.record(
            RequestTrace(
                request_id=turn_id,
                kind="assistant_text",
                conversation_id=conversation_id,
                provider_id=provider,
                started_at=started_at,
                provider_latency_ms=round((time.monotonic() - started) * 1000, 2),
                total_ms=round((time.monotonic() - started) * 1000, 2),
                error=error,
            )
        )

    async def _deterministic(
        self, text: str, conversation_id: str, input_mode: InputMode
    ) -> RouterReply:
        if _TIME.match(text) or _DATE.match(text):
            now = datetime.now(ZoneInfo(self._settings.tars_timezone))
            answer = (
                f"It is {(now.strftime('%I').lstrip('0') or '0')}:{now.strftime('%M %p')}."
                if _TIME.match(text)
                else f"Today is {now.strftime('%A, %B')} {now.day}, {now.year}."
            )
            return await self._persist_pair(
                text, answer, conversation_id, input_mode, "deterministic"
            )

        action_request = build_action_request_from_voice(
            text,
            source=(
                ActionSource.voice_wake_word
                if input_mode is InputMode.voice
                else ActionSource.deterministic
            ),
        )
        if action_request is not None:
            result = await self._actions.submit(action_request)
            answer = _action_result_text(result)
            return await self._persist_pair(
                text, answer, conversation_id, input_mode, "action_runtime"
            )

        # This branch is selected only for the router's explicit state/calculation
        # patterns, so AssistantRouter resolves deterministically and cannot turn
        # an ordinary question into a second model call.
        return await self._assistant_router.handle_text(text, conversation_id)

    async def _advanced(
        self, text: str, *, turn_id: str, conversation_id: str
    ) -> tuple[str, str]:
        display = ""
        provider = "orchestrator"
        async for event in self._orchestrator.handle_text_stream(text, conversation_id):
            if event.get("type") == "delta":
                chunk = str(event.get("text") or "")
                display += chunk
                if chunk:
                    await self._publish(TurnEvent(turn_id=turn_id, type="delta", text=chunk))
            elif event.get("type") == "complete":
                display = str(event.get("display_text") or display)
                message = event.get("message") or {}
                providers = message.get("providers") if isinstance(message, dict) else {}
                if isinstance(providers, dict):
                    provider = str(providers.get("assistant") or provider)
        if not display.strip():
            raise RuntimeError("advanced runtime returned no final response")
        return display, provider

    async def _chart_analysis(self, *, turn_id: str) -> tuple[str, str]:
        latest = await self._hot_charts.get_latest()
        if latest is None or latest.freshness() is Freshness.STALE:
            return (
                "I don't have a fresh chart observation yet. Keep the chart visible for the "
                "background watcher, then ask me again.",
                "hot_chart_state",
            )
        display = latest.analysis.formatted_tars_text()
        await self._publish(TurnEvent(turn_id=turn_id, type="delta", text=display))
        return display, latest.analysis.provider or "hot_chart_state"

    async def _persist_pair(
        self,
        user_text: str,
        assistant_text: str,
        conversation_id: str,
        input_mode: InputMode,
        provider: str,
    ) -> RouterReply:
        conversation_uuid = _conversation_uuid(conversation_id)
        user = AssistantMessage(
            conversation_id=conversation_uuid,
            role=MessageRole.user,
            content=user_text,
            input_mode=input_mode,
        )
        presentation = self._composer.compose(user_text=user_text, display_text=assistant_text)
        assistant = AssistantMessage(
            conversation_id=conversation_uuid,
            role=MessageRole.assistant,
            content=presentation.display_text,
            input_mode=input_mode,
            intent=TurnIntent.DETERMINISTIC.value,
            providers=MessageProviders(assistant=provider),
        )
        await self._persist_message(user)
        await self._persist_message(assistant)
        return RouterReply(
            conversation_id=conversation_id,
            user_message=user,
            assistant_message=assistant,
            presentation=presentation,
        )

    async def _publish(self, event: TurnEvent) -> None:
        # Observation is deliberately best-effort and non-blocking.
        if event.type == "delta" and event.turn_id not in self._first_token_marked:
            recorder = self._voice_recorders.get(event.turn_id)
            if recorder is not None:
                self._first_token_marked.add(event.turn_id)
                try:
                    await recorder.mark("first_response_token")
                except Exception:
                    logger.debug("voice first-token telemetry failed", exc_info=True)
        for queue in tuple(self._subscribers.get(event.turn_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass


def _conversation_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        return uuid5(NAMESPACE_URL, f"tars-conversation:{value}")


def _log_memory_observer_failure(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.warning("non-blocking memory observer failed: %s", error)


def _fingerprint(text: str, conversation_id: str, input_mode: InputMode, speak: bool) -> str:
    payload = json.dumps(
        {
            "text": text,
            "conversation_id": conversation_id,
            "input_mode": input_mode.value,
            "speak": speak,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _action_result_text(result: ActionResult) -> str:
    if result.status is ActionStatus.CONFIRMATION_REQUIRED:
        return result.summary or "That action needs your confirmation before I can continue."
    if result.error and result.status is not ActionStatus.SUCCEEDED:
        return f"{result.summary} {result.error}".strip()
    return result.summary


def _iso_from_epoch_ms(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()


def normalize_transcript(transcript: str) -> str:
    text = transcript.casefold().replace("’", "'")
    # Whisper occasionally inserts an apostrophe or separates the final s.
    text = re.sub(r"(?<=\w)'(?=\w)", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\bhey\s+tar\s+s\b", "hey tars", text)
    return re.sub(r"\s+", " ", text).strip()


def sentence_chunks(speech_text: str) -> list[str]:
    """Return only complete, natural TTS units from final backend speech."""

    normalized = re.sub(r"[ \t]+", " ", speech_text).strip()
    if not normalized:
        return []
    return [
        chunk.strip()
        for chunk in re.split(r"(?<=[.!?])\s+|\n+", normalized)
        if chunk.strip()
    ]
