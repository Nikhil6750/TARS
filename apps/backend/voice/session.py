"""Authoritative realtime voice lifecycle; AssistantTurnController owns execution.

Generation checks guard every asynchronous boundary including playback ACKs.
React/native transport are observers; neither chooses conversational states.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import logging
import statistics
import time
from collections import defaultdict, deque
from enum import Enum
from uuid import uuid4

from app.schemas import InputMode
from assistant.turn_controller import WakePhraseMatcher
from voice.streaming import IncrementalWhisperSTT, LocalStreamingTTS, SherpaPartialEngine, SpeechChunker

logger = logging.getLogger("tars.realtime")


class VoiceState(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    USER_SPEAKING = "USER_SPEAKING"
    ENDPOINTING = "ENDPOINTING"
    THINKING = "THINKING"
    ASSISTANT_SPEAKING = "ASSISTANT_SPEAKING"
    INTERRUPTING = "INTERRUPTING"
    ERROR = "ERROR"


class LatencyMetrics:
    pairs = {
        "speech_ended_to_final_transcript": ("speech_ended", "final_transcript"),
        "speech_ended_to_agent_request_started": ("speech_ended", "agent_request_started"),
        "agent_request_started_to_first_token": ("agent_request_started", "first_token"),
        "first_token_to_first_tts_audio": ("first_token", "first_tts_audio"),
        "first_token_to_first_speech_chunk": ("first_token", "first_speech_chunk"),
        "first_speech_chunk_to_first_tts_audio": ("first_speech_chunk", "first_tts_audio"),
        "first_speech_chunk_to_synth_started": ("first_speech_chunk", "tts_synth_started"),
        "speech_ended_to_first_audible_audio": ("speech_ended", "first_audible_audio"),
        "barge_in_detected_to_playback_stopped": ("barge_in_detected", "playback_stopped"),
    }

    def __init__(self):
        self.turns: dict[str, dict[str, float]] = {}
        self.samples: dict[str, deque] = defaultdict(lambda: deque(maxlen=100))
        self.latest: dict[str, float] = {}
        self._recorded: set[tuple[str, str]] = set()

    def mark(self, turn: str, marker: str, at: float | None = None):
        markers = self.turns.setdefault(turn, {})
        if marker not in markers:
            logger.info("voice_marker turn=%s marker=%s", turn, marker)
        markers.setdefault(marker, time.perf_counter() if at is None else at)
        for key, (start, end) in self.pairs.items():
            if start in markers and end in markers and (turn, key) not in self._recorded:
                ms = round(max(0, markers[end] - markers[start]) * 1000, 2)
                self.latest[key] = ms
                self.samples[key].append(ms)
                self._recorded.add((turn, key))
                logger.info("voice_latency turn=%s metric=%s ms=%s p50=%s n=%s",
                            turn, key, ms, statistics.median(self.samples[key]), len(self.samples[key]))
        if len(self.turns) > 100:
            old = next(iter(self.turns))
            del self.turns[old]
            self._recorded = {item for item in self._recorded if item[0] != old}

    def snapshot(self):
        return {"latest_ms": dict(self.latest), "p50_ms": {
            key: statistics.median(values) for key, values in self.samples.items() if len(values) >= 2
        }, "sample_counts": {key: len(values) for key, values in self.samples.items()},
            "measurement": "backend monotonic; audible/stop use client playback acknowledgements"}


class VoiceSessionController:
    def __init__(self, turns, voice, emit, vad, *, metrics=None, wake_aliases=None, partial_engine=None,
                 context_provider=None):
        self.context_provider = context_provider
        self.turns, self.voice, self.emit = turns, voice, emit
        self.metrics = metrics or LatencyMetrics()
        self.state = VoiceState.IDLE
        self.session_id = uuid4().hex
        self.generation = 0
        self.turn_id = f"{self.session_id}:0"
        self.utterance = 0
        self.closed = False
        self.response_task: asyncio.Task | None = None
        self.tts: LocalStreamingTTS | None = None
        self.tts_task: asyncio.Task | None = None
        self.audio_pending: set[int] = set()
        self.audio_credit = asyncio.Semaphore(2)
        self.audio_sequence = 0
        self.seq = 0
        self.tts_done = False
        self._interrupted: set[str] = set()
        self.history: deque[dict] = deque(maxlen=300)
        self._matcher = WakePhraseMatcher(wake_aliases or ["hey tars", "tars"])
        self.spoken: deque[str] = deque(maxlen=6)
        self.quiet_until = 0.0
        self.stt = IncrementalWhisperSTT(voice.stt, self.on_stt, vad, partial_engine=partial_engine,
                                         busy=self._busy, echo_reference=lambda: " ".join(self.spoken))
        self.provider_status = {"microphone": "DISCONNECTED", "stt": "DISCONNECTED",
                                "tts": "DISCONNECTED", "assistant": "DISCONNECTED"}

    async def send(self, type_: str, **payload):
        self.seq += 1
        event = {"type": type_, "turn_id": self.turn_id, "seq": self.seq, "ts": time.time(),
                 "generation": self.generation, **payload}
        # Diagnostic metadata only; never retain audio blobs.
        if type_ not in {"delta", "metrics"}:
            self.history.append({k: v for k, v in event.items() if k not in {"audio", "response"}})
        await self.emit(event)

    async def transition(self, state: VoiceState):
        if self.closed or self.state == state:
            return
        if state is VoiceState.LISTENING and self.state is VoiceState.ASSISTANT_SPEAKING:
            self.quiet_until = time.monotonic() + 1.2  # speaker tail still reaches the mic
        self.state = state
        await self.send("state", state=state.value)

    async def start(self):
        await self.stt.start()
        # Warm the decoder off the event loop so the first real utterance is not cold.
        asyncio.create_task(self._warm())
        await self.transition(VoiceState.LISTENING)
        await self.send("provider_status", providers=self.provider_status.copy())

    def _busy(self) -> bool:
        return (self.state in {VoiceState.THINKING, VoiceState.ASSISTANT_SPEAKING}
                or bool(self.audio_pending) or time.monotonic() < self.quiet_until)

    async def _warm(self):
        try:
            await self.voice.stt.transcribe(bytes(32000))
        except Exception:
            pass

    async def push_audio(self, frame: bytes):
        if self.closed:
            return
        if self.provider_status["microphone"] != "CONNECTED":
            self.provider_status["microphone"] = "CONNECTED"
            await self.send("provider_status", providers=self.provider_status.copy())
        try:
            await self.stt.push_audio(frame)
        except Exception as exc:
            await self.failure("stt", type(exc).__name__)

    async def failure(self, provider: str, detail: str):
        self.provider_status[provider] = "ERROR"
        await self.send("provider_status", providers=self.provider_status.copy(), detail=detail)
        await self.transition(VoiceState.ERROR)

    async def interrupt(self):
        previous = self.turn_id
        busy = self.state in {VoiceState.THINKING, VoiceState.ASSISTANT_SPEAKING} or bool(self.audio_pending)
        # Invalidate before any await. Stale callbacks can never reclaim ownership.
        self.generation += 1
        self.turn_id = f"{self.session_id}:{self.generation}"
        if self.tts:
            self.tts.flush()
        if self.response_task:
            self.response_task.cancel()
        if self.tts_task:
            self.tts_task.cancel()
        self.audio_pending.clear()
        self.audio_credit = asyncio.Semaphore(2)
        self.tts_done = False
        if busy:
            self.metrics.mark(previous, "barge_in_detected")
            self._interrupted.add(previous)
            if len(self._interrupted) > 100:
                self._interrupted.pop()
            await self.transition(VoiceState.INTERRUPTING)
        await self.send("interrupt", previous_turn_id=previous, status="interrupted")
        await self.turns.cancel_turn(previous)

    async def on_stt(self, event: dict):
        if self.closed:
            return
        kind = event["type"]
        if kind == "speech_started":
            await self.interrupt()
            self.utterance = event["utterance"]
            self.metrics.mark(self.turn_id, "speech_started")
            await self.send("speech_started")
            await self.transition(VoiceState.USER_SPEAKING)
            return
        if self.closed or (event.get("utterance") != self.utterance and not (
                kind == "final_transcript" and event.get("utterance") == self.stt.valid_utterance)):
            return
        if kind == "endpointing":
            await self.transition(VoiceState.ENDPOINTING)
        elif kind == "speech_resumed":
            await self.transition(VoiceState.USER_SPEAKING)
        elif kind == "speech_discarded":
            self.utterance = 0
            if not event.get("silent"):
                await self.transition(VoiceState.LISTENING)
        elif kind == "speech_ended":
            self.metrics.mark(self.turn_id, kind, event["at"])
            await self.send(kind)
        elif kind == "provider_error":
            await self.failure("stt", event["detail"])
        elif kind in {"partial_transcript", "final_transcript"}:
            self.provider_status["stt"] = "CONNECTED" if self.voice.stt.name != "mock" else "DEGRADED"
            await self.send(kind, text=event["text"])
            if kind == "final_transcript":
                self.metrics.mark(self.turn_id, kind)
                text = event["text"].strip()
                # Session is explicitly live/listening; aliases are optional once active.
                match = self._matcher.match(text)
                if match:
                    text = match.command or "Hello"
                if not text:
                    await self.transition(VoiceState.LISTENING)
                    return
                await self.transition(VoiceState.THINKING)
                self.response_task = asyncio.create_task(self._respond(text, self.generation, self.turn_id))

    async def _respond(self, text: str, generation: int, turn_id: str):
        self.spoken.clear()
        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=8)
        chunker = SpeechChunker()
        saw_delta = False
        async def text_stream():
            while True:
                item = await queue.get()
                if item is None:
                    return
                yield item

        async def audio_chunk(result):
            await self.audio_credit.acquire()
            if generation != self.generation or self.closed:
                return
            self.audio_sequence += 1
            seq = self.audio_sequence
            self.audio_pending.add(seq)
            self.metrics.mark(turn_id, "first_tts_audio")
            self.provider_status["tts"] = "CONNECTED" if self.voice.tts.name != "mock" else "DEGRADED"
            await self.transition(VoiceState.ASSISTANT_SPEAKING)
            await self.send("audio_chunk", sequence=seq,
                            audio=base64.b64encode(result.audio).decode("ascii"))

        async def complete():
            if generation != self.generation:
                return
            self.tts_done = True
            await self.send("tts_complete")
            if not self.audio_pending:
                await self.transition(VoiceState.LISTENING)

        tts = LocalStreamingTTS(self.voice.tts, audio_chunk, complete,
                                on_synth_started=lambda: self.metrics.mark(turn_id, "tts_synth_started"))
        self.tts = tts
        async def synthesize():
            try:
                await tts.start(text_stream())
            except Exception as exc:
                if generation == self.generation:
                    await self.failure("tts", type(exc).__name__)
                    # Drain speech so an unavailable TTS cannot block response display.
                    async for _ in text_stream():
                        pass
        self.tts_task = asyncio.create_task(synthesize())
        try:
            self.metrics.mark(turn_id, "agent_request_started")
            async with asyncio.timeout(120):
                context = self.context_provider() if self.context_provider else ""
                if inspect.isawaitable(context):
                    context = await context
                async for event in self.turns.stream_text(
                    (context + " " + text) if context else text, turn_id=turn_id, conversation_id=self.session_id,
                    input_mode=InputMode.voice, speak=False
                ):
                    if generation != self.generation or self.closed:
                        return
                    if event.type == "delta" and event.text:
                        saw_delta = True
                        self.metrics.mark(turn_id, "first_token")
                        await self.send("delta", text=event.text)
                        for chunk in chunker.feed(event.text):
                            self.spoken.append(chunk)
                            self.metrics.mark(turn_id, "first_speech_chunk")
                            await queue.put(chunk)
                    elif event.type == "complete" and event.response:
                        response = event.response
                        if response.status.value == "failed":
                            await self.failure("assistant", "Assistant unavailable")
                        else:
                            self.provider_status["assistant"] = "CONNECTED" if response.provider != "mock" else "DEGRADED"
                        if not saw_delta:
                            self.metrics.mark(turn_id, "first_token")
                        for chunk in chunker.feed("" if saw_delta else response.speech_text, final=True):
                            self.spoken.append(chunk)
                            await queue.put(chunk)
                        await self.send("response_complete", response=response.model_dump(mode="json"))
            await queue.put(None)
            await self.tts_task
            await self.send("provider_status", providers=self.provider_status.copy())
            await self.send("metrics", **self.metrics.snapshot())
        except asyncio.CancelledError:
            tts.cancel()
            raise
        except Exception as exc:
            tts.cancel()
            if generation == self.generation:
                await self.failure("assistant", type(exc).__name__)
                await self.turns.cancel_turn(turn_id)

    async def playback(self, message: dict):
        turn = message.get("turn_id")
        kind = message.get("type")
        if kind == "playback_stopped" and turn in self._interrupted:
            self.metrics.mark(turn, "playback_stopped")
            self._interrupted.discard(turn)
            await self.send("metrics", **self.metrics.snapshot())

            return
        if turn != self.turn_id or self.closed:
            return
        sequence = message.get("sequence")
        if sequence not in self.audio_pending:
            return
        if kind == "audio_started":
            self.metrics.mark(turn, "first_audible_audio")
        elif kind in {"audio_done", "audio_error"}:
            self.audio_pending.remove(sequence)
            self.audio_credit.release()
            if kind == "audio_error":
                await self.failure("playback", "Native webview playback failed")
            elif self.tts_done and not self.audio_pending:
                await self.transition(VoiceState.LISTENING)
            await self.send("metrics", **self.metrics.snapshot())

    async def speak_alert(self, text: str):
        # Proactive speech never takes the microphone turn away from a user.
        if self.closed or self.state is not VoiceState.LISTENING or self.stt.active:
            return
        await self.interrupt()
        generation = self.generation
        async def speak():
            try:
                chunks = SpeechChunker().feed(text, final=True)
                for chunk in chunks[:3]:
                    result = await asyncio.wait_for(self.voice.tts.synthesize(chunk), 20)
                    if generation != self.generation or self.closed:
                        return
                    await self.audio_credit.acquire()
                    if generation != self.generation:
                        return
                    self.audio_sequence += 1
                    self.audio_pending.add(self.audio_sequence)
                    await self.transition(VoiceState.ASSISTANT_SPEAKING)
                    await self.send("audio_chunk", sequence=self.audio_sequence,
                                    audio=base64.b64encode(result.audio).decode("ascii"))
                self.tts_done = True
                if not self.audio_pending:
                    await self.transition(VoiceState.LISTENING)
            except Exception as exc:
                if generation == self.generation:
                    await self.failure("tts", type(exc).__name__)
        self.response_task = asyncio.create_task(speak())

    async def close(self):
        self.closed = True
        self.generation += 1
        if self.tts:
            self.tts.cancel()
        for task in (self.response_task, self.tts_task):
            if task:
                task.cancel()
        await self.turns.cancel_turn(self.turn_id)
        await self.stt.stop()
        await asyncio.gather(*(t for t in (self.response_task, self.tts_task) if t), return_exceptions=True)
        self.state = VoiceState.IDLE
