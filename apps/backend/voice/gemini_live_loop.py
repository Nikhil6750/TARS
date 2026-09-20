"""Gemini Live realtime voice loop -- the primary production automatic-
listening owner, superseding the local SpeechRecognition+faster-whisper
loop (voice/voice_loop.py, DELETE-LATER once this is physically proven).

Gemini Live is EARS ONLY:
  - realtime microphone audio streaming
  - server-side VAD / speech turn detection
  - input speech transcription (en-US)

It is never TARS's voice and never its brain. Gemini-generated audio is
never used. The connected model (a native-audio-preview Live model) does
not accept `response_modalities=["TEXT"]` at all -- confirmed by a live
connection attempt, which the API rejects outright -- so `AUDIO` is still
requested at the session level (a model requirement, not a choice). The
actual guarantee that the user never hears it is at the code level, not
the API level: this module has no code path that ever reads or plays
`message.server_content.model_turn` -- only `content.input_transcription`
and `content.turn_complete` are ever consumed. Gemini may still generate
audio bytes server-side (wasted quota, never a problem observed), but
nothing in this process ever touches them. TARS remains authoritative for
everything downstream: the transcribed user utterance is executed through
the existing, unmodified `AssistantTurnController.stream_text` -- routing
(deterministic / FAST_CONVERSATION / Claude+Codex / actions), permissions,
memory, chart analysis, and quant_brain boundaries are all untouched.
TARS's reply is streamed sentence-by-sentence to `_fast_tts` (Windows SAPI,
~150-300ms per sentence, measured) as soon as each sentence is complete,
not synthesized-then-played as one block -- the first sentence is audible
while the rest of a longer reply is still being generated. Kokoro (the
prior default, ~1-4s per sentence, CPU-bound neural synthesis) is kept as
`self._voice_providers.tts`, used only as the OFFLINE_QUALITY_FALLBACK if
SAPI synthesis fails. Speech is never sent back into Gemini for synthesis.

(Earlier revisions of this module routed TARS's reply through Gemini's own
audio generation via a "TARS_SPEAK:" text prompt. That coupling proved
unreliable in physical testing -- Gemini's own generative model, not a
fixed playback of TARS's exact text, occasionally answered in a different
language and looped. This revision removes that coupling entirely rather
than patching around it: Gemini never generates the audio a user hears.)

Requires GEMINI_API_KEY or GOOGLE_API_KEY in the process environment
(read directly, never through Settings/.env, per the explicit "API key
from environment only" requirement).

Threading note (load-bearing, found via physical crash evidence): WASAPI
(AUDIOSES.DLL) is not safe to access from a rotating thread pool.
`asyncio.to_thread()` per audio chunk -- one PyAudio input stream read via
the default executor, one output stream write via the same -- produced two
reproducible STATUS_ACCESS_VIOLATION crashes in AUDIOSES.DLL during
physical testing (confirmed via Windows Event Viewer Application Error
records, not guessed). `_MicCaptureThread` and `_PlaybackThread` each own
their PyAudio stream on one dedicated, persistent OS thread for the whole
GeminiLiveLoop lifetime (not recreated per reconnect either, which cuts
WASAPI stream churn during reconnect storms) and hand data across into
asyncio via thread-safe queues.

Hard half-duplex: `self._playback_active` is the ONE authoritative flag
gating microphone forwarding, checked at the actual send boundary in
`send_mic_audio()`. It and `self._turn_phase` live on the instance (not as
per-session local closures) specifically so an in-flight turn survives a
Gemini session reconnect without losing the gate -- see their definitions
in `__init__` for the full reasoning.
"""
from __future__ import annotations

import asyncio
import logging
import os
import queue as sync_queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
import pyaudio
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from assistant.turn_controller import (
    TurnStatus,
    WakeMatch,
    WakePhraseMatcher,
    normalize_transcript,
    sentence_chunks,
)
from voice.errors import VoiceProviderError
from voice.interfaces import SynthesisResult


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _ms(end: float | None, start: float | None) -> str:
    """Millisecond delta between two monotonic timestamps, or "n/a" if
    either stage never happened this turn (e.g. no VAD ACTIVITY_END
    observed) -- never fabricates a number for a stage that didn't occur."""
    if end is None or start is None:
        return "n/a"
    return f"{(end - start) * 1000:.1f}"


def _resolve_command(
    user_text: str, wake_match: WakeMatch | None, *, follow_up_active: bool
) -> str | None:
    """Decides whether a recognized utterance should execute as a TARS
    command, and what text to route if so. Returns None if the turn must
    be dropped outright -- no wake phrase found AND no open follow-up
    window -- which is the fix for ambient room speech (TV, other
    conversations, background noise) creating spurious TARS turns.
    A wake phrase always takes priority over the follow-up window (saying
    "Hey TARS" mid-window still works exactly as it always did)."""
    if wake_match is not None:
        return wake_match.command if wake_match.command else user_text
    if follow_up_active:
        return user_text
    return None


def _looks_like_echo(candidate: str, last_tars_response: str | None) -> bool:
    """Secondary echo protection (the primary defense is the playback_active
    mic-send gate in send_mic_audio -- this only matters if that somehow let
    something through). A transcript that closely matches TARS's own most
    recently spoken response is almost certainly TARS hearing itself, not a
    real new command."""
    if not last_tars_response:
        return False
    candidate_norm = normalize_transcript(candidate)
    response_norm = normalize_transcript(last_tars_response)
    if not candidate_norm or not response_norm:
        return False
    if candidate_norm == response_norm:
        return True
    shorter, longer = (
        (candidate_norm, response_norm)
        if len(candidate_norm) <= len(response_norm)
        else (response_norm, candidate_norm)
    )
    return len(shorter) >= 8 and shorter in longer


# Streaming-TTS chunking: a growing reply is only safe to speak sentence by
# sentence once each sentence is DONE (hit terminal punctuation) -- the
# last item sentence_chunks() returns from partial text may just be an
# in-progress fragment that keeps growing. The word-count valve exists so
# one unusually long run-on sentence with no punctuation yet doesn't leave
# TARS silent for the whole reply while streaming.
MAX_WORDS_BEFORE_FORCED_CHUNK = 20


def _pending_speakable_chunks(
    all_chunks: list[str], already_spoken_count: int, *, is_final: bool
) -> list[str]:
    """`all_chunks` is sentence_chunks() run fresh on the full text
    generated so far. Returns only the chunks beyond `already_spoken_count`
    that are safe to speak now -- everything, if `is_final` (no more text
    is coming); otherwise all but a possibly-still-growing last fragment,
    unless it's already long enough that waiting longer would mean too
    much dead air."""
    if not all_chunks:
        return []
    safe_count = len(all_chunks)
    if not is_final:
        last = all_chunks[-1]
        if not last.rstrip().endswith((".", "!", "?")) and len(last.split()) < MAX_WORDS_BEFORE_FORCED_CHUNK:
            safe_count -= 1
    return all_chunks[already_spoken_count:safe_count]


logger = logging.getLogger("tars.voice.gemini_live_loop")

INPUT_SAMPLE_RATE = 16000  # Gemini Live's required input rate
# The fixed rate _PlaybackThread's device stream is opened at for the
# whole GeminiLiveLoop lifetime. Both TTS providers get resampled to this
# if their actual output rate differs (SAPI measured at 22050Hz, Kokoro at
# 24000Hz -- neither is assumed, both read back from the real WAV header).
OUTPUT_SAMPLE_RATE = 24000
CHUNK_FRAMES = 1024
# Reconnect policy, deliberately conservative during development: capped
# attempts with exponential backoff (never more than one attempt per
# BASE_RECONNECT_DELAY_SECONDS, growing from there), and RESOURCE_EXHAUSTED
# specifically stops retrying immediately rather than consuming the
# attempt budget -- retrying a quota rejection cannot succeed and only
# makes the exhaustion worse.
MAX_RECONNECT_ATTEMPTS = 3
BASE_RECONNECT_DELAY_SECONDS = 5.0
MIC_QUEUE_MAX_CHUNKS = 64  # ~1.4s of native-rate audio; drop, don't block, if exceeded
# Upper bound on waiting for Kokoro's local playback to confirm every
# chunk was physically written to the output device, so a turn can never
# permanently wedge the loop out of "listening" if the audio device stalls.
PLAYBACK_TIMEOUT_SECONDS = 20.0
# Half-duplex: how long to keep withholding mic audio from Gemini after
# TARS's own audio is confirmed finished playing, before resuming normal
# listening. Covers acoustic tail (speaker audio still physically decaying
# in the room / any output buffering) so the mic doesn't start streaming
# TARS's own trailing speech back into Gemini the instant playback ends.
POST_SPEECH_COOLDOWN_SECONDS = 0.5
# Only one user turn may be active at a time (see _run_session's turn_phase
# gate). Not currently configurable -- named as a constant because it's a
# hard product invariant, not a tuning knob.
MAX_ACTIVE_USER_TURNS = 1
# A turn's transcript must start executing within this long of becoming
# available (turn_complete), or it's dropped rather than run late. In the
# current architecture this should never trigger -- the task is created
# synchronously in the same event-loop tick the transcript finalizes -- but
# it's the explicit, auditable backstop against ever answering a stale
# request, not merely an assumption that scheduling is always instant.
STALE_TURN_MAX_AGE_SECONDS = 2.0
# How long after a valid (wake-triggered or in-window) response a follow-up
# utterance is accepted WITHOUT repeating the wake phrase -- "Explain that
# more simply" right after an answer shouldn't require "Hey TARS" again.
# Ambient room speech with no wake phrase is dropped outright once this
# window has closed (see handle_user_utterance's DROPPED_NO_WAKE path).
FOLLOW_UP_WINDOW_SECONDS = 15.0

SYSTEM_INSTRUCTION = (
    "You are the speech-input layer for TARS.\n"
    "Always interpret speech as English unless the user explicitly requests "
    "another language.\n"
    "Do not answer the user.\n"
    "Do not conduct a conversation.\n"
    "Your job is only to receive speech and provide transcription/turn "
    "boundaries."
)


@dataclass
class GeminiLiveStatus:
    state: str = "stopped"  # stopped|starting|voice_off|listening|user_speaking|processing|speaking
    device_name: str | None = None
    error: str | None = None
    # The one authoritative backend flag for manual listening mode --
    # frontend must read this, never guess/derive it locally. False by
    # default on every app start (see GeminiLiveLoop.__init__).
    manual_listening_enabled: bool = False
    # Runtime evidence for the actual mic -> Gemini -> transcript -> TARS ->
    # Kokoro -> speaker chain, so a stuck/broken stage is diagnosable from
    # live state rather than guessed. All timestamps are ISO 8601 UTC or
    # None if that stage has never happened yet this run.
    connected: bool = False
    mic_streaming: bool = False
    last_audio_sent_at: str | None = None
    last_input_transcript: str | None = None
    last_turn_received_at: str | None = None
    last_tars_response: str | None = None
    last_audio_response_at: str | None = None
    last_error: str | None = None
    # Which TTS actually produced the audio last heard -- "elevenlabs",
    # "sapi", or "kokoro". Diagnostic only; never drives behavior.
    tts_provider: str | None = None
    # Diagnostic only, does not change `state`'s wire values (nothing
    # downstream reads this to drive UI): True when the next utterance
    # needs a wake phrase ("Hey TARS"/"TARS"), False while the 15s
    # follow-up window from the last valid response is still open.
    wake_required: bool = True
    last_dropped_no_wake_transcript: str | None = None


def _downmix_to_pcm16(raw_float32_stereo: bytes) -> bytes:
    stereo = np.frombuffer(raw_float32_stereo, dtype="<f4").reshape(-1, 2)
    mono = (stereo[:, 0] + stereo[:, 1]) / 2.0
    return (np.clip(mono, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


# PortAudio/WASAPI stream *initialization* is not safe to call concurrently
# from two threads -- physically reproduced as a hard process segfault when
# an input and output stream were opened from separate threads at nearly
# the same moment (isolated two-line repro, no asyncio involved). Reads and
# writes are safe on their own dedicated threads once open; only the
# PyAudio()/.open()/.close()/.terminate() calls themselves need to be
# serialized across the whole process.
_PORTAUDIO_LOCK = threading.Lock()


class _MicCaptureThread:
    """Owns the PyAudio input stream on one dedicated OS thread for the
    entire GeminiLiveLoop lifetime. Reads happen only on this thread;
    chunks are handed to the asyncio world via call_soon_threadsafe."""

    def __init__(
        self,
        *,
        device_index: int,
        sample_rate: int,
        event_loop: asyncio.AbstractEventLoop,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._device_index = device_index
        self.sample_rate = sample_rate
        self._loop = event_loop
        self._on_error = on_error
        self.chunks: asyncio.Queue[bytes] = asyncio.Queue(maxsize=MIC_QUEUE_MAX_CHUNKS)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="tars-gemini-mic-capture", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        with _PORTAUDIO_LOCK:
            audio = pyaudio.PyAudio()
            try:
                stream = audio.open(
                    input_device_index=self._device_index,
                    channels=2,
                    format=pyaudio.paFloat32,
                    rate=self.sample_rate,
                    frames_per_buffer=CHUNK_FRAMES,
                    input=True,
                )
            except Exception as exc:
                logger.exception("_MicCaptureThread: failed to open input stream")
                if self._on_error is not None:
                    self._on_error(f"mic stream open failed: {exc}")
                audio.terminate()
                return
        try:
            while not self._stop.is_set():
                try:
                    raw = stream.read(CHUNK_FRAMES, exception_on_overflow=False)
                except Exception as exc:
                    logger.exception("_MicCaptureThread: stream read failed")
                    if self._on_error is not None:
                        self._on_error(f"mic stream read failed: {exc}")
                    break
                self._loop.call_soon_threadsafe(self._enqueue, raw)
        finally:
            with _PORTAUDIO_LOCK:
                stream.stop_stream()
                stream.close()
                audio.terminate()

    def _enqueue(self, raw: bytes) -> None:
        try:
            self.chunks.put_nowait(raw)
        except asyncio.QueueFull:
            pass  # drop a stale chunk rather than fall behind real time


class _PlaybackThread:
    """Owns the PyAudio output stream on one dedicated OS thread for the
    entire GeminiLiveLoop lifetime -- the sole path for TARS's own speech
    (Kokoro-synthesized PCM16), never Gemini's audio. `write()` enqueues one
    chunk without blocking, so the caller can synthesize sentence N+1 while
    sentence N is already playing (pipelined, not synthesize-everything-
    then-play-everything -- that serialization was adding several seconds
    of dead silence before any audio started on multi-sentence replies).
    `wait_for_drain()` blocks until every written chunk has actually been
    played, the real completion signal the half-duplex mic gate needs."""

    def __init__(self) -> None:
        self._queue: sync_queue.Queue[bytes | None] = sync_queue.Queue()
        self._thread = threading.Thread(
            target=self._run, name="tars-speaker-playback", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def write(self, pcm_bytes: bytes) -> None:
        """Enqueue one chunk for playback immediately -- does not block on
        it actually being played. Call `wait_for_drain()` to know when
        every chunk written so far has been."""
        self._queue.put(pcm_bytes)

    async def wait_for_drain(self) -> None:
        """Blocks (off-thread, safe to await) until every chunk written so
        far has actually been written to the audio device -- the real
        completion signal the half-duplex mic gate needs, via the stdlib
        queue.Queue task-tracking protocol."""
        await asyncio.to_thread(self._queue.join)

    def stop(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        with _PORTAUDIO_LOCK:
            audio = pyaudio.PyAudio()
            try:
                stream = audio.open(
                    format=pyaudio.paInt16, channels=1, rate=OUTPUT_SAMPLE_RATE, output=True
                )
            except Exception:
                logger.exception("_PlaybackThread: failed to open output stream")
                audio.terminate()
                return
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    self._queue.task_done()
                    break
                try:
                    stream.write(item)
                except Exception:
                    logger.exception("_PlaybackThread: stream write failed")
                self._queue.task_done()
        finally:
            with _PORTAUDIO_LOCK:
                stream.stop_stream()
                stream.close()
                audio.terminate()


class GeminiLiveLoop:
    """Owns realtime mic capture/streaming/transcription via Gemini Live
    (ears only) and TARS's spoken output via FAST_TTS/Kokoro (mouth only).
    One instance per backend process."""

    def __init__(
        self, *, settings, controller, event_loop: asyncio.AbstractEventLoop, voice_providers
    ) -> None:
        self._settings = settings
        self._controller = controller
        self._main_event_loop = event_loop
        # Lazily read `.tts` at use time, never captured once here: it
        # starts as a mock and is swapped in place for the real Kokoro
        # provider once VoiceProviders.load() finishes (see app/voice_state.py)
        # -- start() only runs after that swap has already happened, but
        # holding the object (not a snapshot of .tts) is what makes that
        # true regardless of ordering.
        self._voice_providers = voice_providers
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._resumption_handle: str | None = None
        # ONE authoritative turn/playback state, deliberately on the
        # instance rather than as a local variable inside _run_session:
        # a Gemini session can reconnect (session duration limits, network
        # blips) while a turn is still mid-flight (TARS still executing, or
        # Kokoro still speaking). The task handling that turn keeps running
        # across the reconnect and must keep gating the *new* session's mic
        # forwarding correctly -- which only works if the state it mutates
        # is shared, not scoped to the now-dead old session's closure.
        self._turn_phase = "listening"  # listening|processing|speaking
        self._playback_active = False
        # Guards against a stale/superseded turn's response ever being
        # delivered/spoken -- defense in depth on top of the turn_phase gate
        # above, which should already make two turns overlapping impossible.
        self._active_turn_id: str | None = None
        # Wake-required activation: monotonic deadline until which a
        # follow-up utterance is accepted WITHOUT a wake phrase (see
        # FOLLOW_UP_WINDOW_SECONDS). None/expired means the next utterance
        # must start with a wake phrase or it is dropped outright --
        # otherwise ambient room speech (TV, other conversations) creates
        # a real TARS turn, which is exactly the bug this guards against.
        self._follow_up_window_until: float | None = None
        # Manual listening mode (replaces always-on ambient listening for
        # now): mic audio is forwarded to Gemini, and speech is accepted
        # with no wake phrase required, ONLY while this is True. False by
        # default on every app start -- Gemini Live may connect in the
        # background, but nothing the mic hears reaches it until the user
        # explicitly presses Start Listening. `_mic_capture` is set once
        # in _async_main (the same instance used for the whole process
        # lifetime) so start/stop, called from outside _run_session's
        # closures (the voice router's HTTP handlers), can flush it.
        self._manual_listening_enabled = False
        self._pending_text = ""
        self._mic_capture: _MicCaptureThread | None = None
        self.status = GeminiLiveStatus()
        # Gemini transcribes literally, including an optional "Hey
        # TARS"/"TARS" prefix the user may say out of habit even though
        # this mode's server-side VAD (not a wake phrase) is the turn
        # boundary. Reusing the exact same matcher the native path uses --
        # not a second wake system -- strips it before routing so
        # deterministic commands (date/time, "open X", ...) aren't broken
        # by an unstripped prefix their anchored regexes never see past.
        self._wake_matcher = WakePhraseMatcher(settings.wake_alias_list)
        # FAST_TTS: the hot-path voice. ~150-300ms per sentence measured on
        # this machine (Windows SAPI, real-time-capable, non-neural) vs
        # Kokoro's ~1-4s -- see voice/providers/sapi_tts.py's docstring for
        # the measurements. Built once here (not per-turn) since SAPI
        # engine/voice selection has its own one-time overhead. None (not a
        # crash) if unavailable -- e.g. non-Windows -- in which case every
        # turn falls straight to the Kokoro OFFLINE_QUALITY_FALLBACK below.
        self._fast_tts = None
        try:
            from voice.providers.sapi_tts import SapiTTSProvider

            self._fast_tts = SapiTTSProvider()
        except Exception:
            logger.warning(
                "GeminiLiveLoop: FAST_TTS (SAPI) unavailable -- every turn will use "
                "the slower Kokoro OFFLINE_QUALITY_FALLBACK",
                exc_info=True,
            )

        # ElevenLabs: the premium/default connected voice, tried before
        # FAST_TTS on every turn -- see its module docstring for the
        # streaming protocol and PART I of the integration spec this was
        # built against for the fallback contract (ElevenLabs -> SAPI ->
        # Kokoro, never a retry loop). None if ELEVENLABS_TTS_ENABLED is
        # false or the key/voice ID aren't configured; every turn then
        # falls straight to FAST_TTS, same as today.
        self._elevenlabs_tts = None
        if settings.elevenlabs_tts_enabled:
            try:
                from voice.providers.elevenlabs_tts import ElevenLabsTTSProvider

                candidate = ElevenLabsTTSProvider(
                    api_key=settings.elevenlabs_api_key,
                    voice_id=settings.elevenlabs_voice_id,
                    model_id=settings.elevenlabs_model_id,
                    sample_rate=OUTPUT_SAMPLE_RATE,
                )
                if candidate.is_available:
                    self._elevenlabs_tts = candidate
                else:
                    logger.info(
                        "GeminiLiveLoop: ElevenLabs not configured "
                        "(ELEVENLABS_API_KEY/ELEVENLABS_VOICE_ID absent) -- using "
                        "FAST_TTS (SAPI) as the primary voice"
                    )
            except Exception:
                logger.warning(
                    "GeminiLiveLoop: ElevenLabs TTS unavailable -- using FAST_TTS "
                    "(SAPI) as the primary voice",
                    exc_info=True,
                )

    def _idle_status_label(self) -> str:
        """What `self.status.state` should read whenever the loop is
        connected and idle (not mid-VAD-activity/processing/speaking) --
        "listening" if manual listening is on, "voice_off" if not. Never
        used while a turn is actually active; those states (user_speaking/
        processing/speaking) are unaffected by manual listening."""
        return "listening" if self._manual_listening_enabled else "voice_off"

    def _flush_mic_queue(self) -> None:
        """Drops any mic audio already captured but not yet sent/consumed
        -- used on every Start/Stop so a stale buffer from before the
        toggle can never leak into the new state."""
        mic_capture = self._mic_capture
        if mic_capture is None:
            return
        drained = 0
        while not mic_capture.chunks.empty():
            try:
                mic_capture.chunks.get_nowait()
                drained += 1
            except asyncio.QueueEmpty:
                break
        if drained:
            logger.info(
                "GeminiLiveLoop: flushed %d stale mic chunk(s) on manual-listening toggle",
                drained,
            )

    async def start_manual_listening(self) -> None:
        """Turns manual listening ON: mic audio starts being forwarded to
        Gemini (enforced at the send boundary in send_mic_audio) and
        speech is accepted without a wake phrase. Flushes stale buffered
        mic audio first so the very first utterance after pressing Start
        is never contaminated by audio captured while OFF."""
        self._manual_listening_enabled = True
        self.status.manual_listening_enabled = True
        self._pending_text = ""
        self._flush_mic_queue()
        if self._turn_phase == "listening":
            self.status.state = self._idle_status_label()
        logger.info("GeminiLiveLoop: manual listening ENABLED")

    async def stop_manual_listening(self) -> None:
        """Turns manual listening OFF: mic audio stops being forwarded to
        Gemini IMMEDIATELY (same send-boundary gate), any in-progress
        partial transcript is discarded, the follow-up window closes, and
        queued mic frames are flushed. Does NOT touch a turn already in
        flight -- TARS finishes speaking normally; only the idle state it
        returns to afterward changes (Voice Off, not Listening), since
        _finish_turn/VAD handling read _idle_status_label() fresh."""
        self._manual_listening_enabled = False
        self.status.manual_listening_enabled = False
        self._pending_text = ""
        self._follow_up_window_until = None
        self._flush_mic_queue()
        if self._turn_phase == "listening":
            self.status.state = self._idle_status_label()
        logger.info("GeminiLiveLoop: manual listening DISABLED")

    async def _synthesize_chunk(self, text: str) -> tuple[SynthesisResult | None, str]:
        """FAST_TTS first; Kokoro (self._voice_providers.tts, the same
        shared, proven provider the native wake-word path uses) only if
        that fails. Returns (None, "") (never raises) if both fail -- the
        caller skips that chunk rather than losing the whole turn over one
        bad sentence. This is the SAPI/Kokoro fallback tier -- see
        _speak_chunk for where ElevenLabs is tried first."""
        if self._fast_tts is not None:
            try:
                return await self._fast_tts.synthesize(text), self._fast_tts.name
            except VoiceProviderError as exc:
                logger.warning(
                    "GeminiLiveLoop: FAST_TTS (SAPI) failed for one chunk, falling "
                    "back to Kokoro: %s",
                    exc,
                )
        try:
            return await self._voice_providers.tts.synthesize(text), self._voice_providers.tts.name
        except VoiceProviderError as exc:
            logger.warning(
                "GeminiLiveLoop: Kokoro synthesis also failed for one chunk, "
                "skipping it: %s",
                exc,
            )
            return None, ""

    async def _stream_response(self, command_text: str, turn_id: str):
        """Bridges AssistantTurnController.stream_text() -- an async
        generator that must run on the main FastAPI event loop, where the
        controller's locks/state live -- onto this loop's own thread,
        yielding each TurnEvent as it arrives rather than after the whole
        turn completes. This is what makes pipelined TTS possible: the
        caller can start speaking the first sentence while the rest of a
        longer answer is still being generated."""
        from app.schemas import InputMode

        queue: asyncio.Queue = asyncio.Queue()
        this_loop = asyncio.get_running_loop()

        async def _pump() -> None:
            try:
                async for event in self._controller.stream_text(
                    command_text,
                    turn_id=turn_id,
                    conversation_id="gemini-live",
                    input_mode=InputMode.voice,
                    speak=False,
                ):
                    this_loop.call_soon_threadsafe(queue.put_nowait, ("event", event))
            except Exception as exc:
                this_loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
            else:
                this_loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

        asyncio.run_coroutine_threadsafe(_pump(), self._main_event_loop)

        while True:
            kind, payload = await queue.get()
            if kind == "event":
                yield payload
            elif kind == "error":
                raise payload
            else:
                return

    def start(self) -> None:
        if self._thread is not None:
            return
        self._running.set()
        self._thread = threading.Thread(target=self._run, name="tars-gemini-live", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_main())
        finally:
            loop.close()

    async def _async_main(self) -> None:
        from voice.mic_device import resolve_default_input_device

        self.status.state = "starting"
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            logger.error("GeminiLiveLoop: no GEMINI_API_KEY/GOOGLE_API_KEY in environment")
            self.status.state = "stopped"
            self.status.error = "no API key"
            return

        try:
            mic_info = resolve_default_input_device()
        except Exception as exc:
            logger.exception("GeminiLiveLoop: could not resolve a microphone device")
            self.status.state = "stopped"
            self.status.error = str(exc)
            return
        self.status.device_name = mic_info.name

        def _record_mic_error(message: str) -> None:
            self.status.mic_streaming = False
            self.status.last_error = f"{_now_iso()} mic_capture: {message}"

        mic_capture = _MicCaptureThread(
            device_index=mic_info.device_index,
            sample_rate=mic_info.sample_rate,
            event_loop=asyncio.get_running_loop(),
            on_error=_record_mic_error,
        )
        self._mic_capture = mic_capture
        playback = _PlaybackThread()
        mic_capture.start()
        playback.start()

        client = genai.Client(api_key=api_key)

        attempt = 0
        try:
            while self._running.is_set():
                if attempt >= MAX_RECONNECT_ATTEMPTS:
                    logger.error(
                        "GeminiLiveLoop: exhausted %d connection attempts, stopping "
                        "automatic reconnect (not retrying further)",
                        MAX_RECONNECT_ATTEMPTS,
                    )
                    self.status.error = f"gave up after {MAX_RECONNECT_ATTEMPTS} attempts"
                    self._running.clear()
                    break

                attempt += 1
                config = types.LiveConnectConfig(
                    # AUDIO is a model requirement, not a choice: this
                    # native-audio-preview model rejects
                    # response_modalities=["TEXT"] outright (confirmed via
                    # a live connect attempt). The real guarantee that the
                    # user never hears Gemini's voice is that this module
                    # never reads or plays `content.model_turn` anywhere --
                    # see the module docstring.
                    response_modalities=["AUDIO"],
                    system_instruction=SYSTEM_INSTRUCTION,
                    input_audio_transcription=types.AudioTranscriptionConfig(
                        language_codes=["en-US"]
                    ),
                    realtime_input_config=types.RealtimeInputConfig(
                        automatic_activity_detection=types.AutomaticActivityDetection(
                            disabled=False
                        ),
                    ),
                    session_resumption=types.SessionResumptionConfig(
                        handle=self._resumption_handle
                    ),
                    # Deliberately no `tools` -- no Search grounding, no
                    # function calling. This session only ever does
                    # mic -> transcript/turn boundary. Nothing is ever sent
                    # back to it.
                )
                try:
                    async with client.aio.live.connect(
                        model=self._settings.gemini_live_model, config=config
                    ) as session:
                        logger.info(
                            "GEMINI LIVE CONNECTED (ears-only) device=%s model=%s mode=%s "
                            "resumed=%s attempt=%d/%d",
                            mic_info.name,
                            self._settings.gemini_live_model,
                            self._settings.voice_mode,
                            self._resumption_handle is not None,
                            attempt,
                            MAX_RECONNECT_ATTEMPTS,
                        )
                        self.status.state = self._idle_status_label()
                        self.status.error = None
                        self.status.connected = True
                        attempt = 0  # reset the budget on every successful connect
                        await self._run_session(session, mic_capture, playback)
                except genai_errors.APIError as exc:
                    self.status.connected = False
                    self.status.mic_streaming = False
                    message = (exc.message or str(exc)).lower()
                    self.status.last_error = f"{_now_iso()} connect: {exc}"
                    if "exhaust" in message or "quota" in message or exc.code == 1011:
                        logger.error(
                            "GeminiLiveLoop: RESOURCE_EXHAUSTED -- stopping immediately, "
                            "not retrying: %s",
                            exc,
                        )
                        self.status.state = "stopped"
                        self.status.error = f"quota exhausted: {exc}"
                        self._running.clear()
                        break
                    self.status.state = "starting"
                    logger.exception(
                        "GeminiLiveLoop: session error (attempt %d/%d), backing off",
                        attempt,
                        MAX_RECONNECT_ATTEMPTS,
                    )
                    self.status.error = f"session error, retrying ({attempt}/{MAX_RECONNECT_ATTEMPTS})"
                    await asyncio.sleep(BASE_RECONNECT_DELAY_SECONDS * (2 ** (attempt - 1)))
                except Exception as exc:
                    self.status.connected = False
                    self.status.mic_streaming = False
                    self.status.state = "starting"
                    self.status.last_error = f"{_now_iso()} connect: {exc}"
                    logger.exception(
                        "GeminiLiveLoop: session error (attempt %d/%d), backing off",
                        attempt,
                        MAX_RECONNECT_ATTEMPTS,
                    )
                    self.status.error = f"session error, retrying ({attempt}/{MAX_RECONNECT_ATTEMPTS})"
                    await asyncio.sleep(BASE_RECONNECT_DELAY_SECONDS * (2 ** (attempt - 1)))
        finally:
            self.status.connected = False
            self.status.mic_streaming = False
            mic_capture.stop()
            playback.stop()

        self.status.state = "stopped"

    async def _run_session(
        self, session, mic_capture: _MicCaptureThread, playback: _PlaybackThread
    ) -> None:
        """One live connection's lifetime: streams mic audio in (subject to
        the half-duplex gate) and drives listening -> processing (TARS) ->
        speaking (Kokoro) off input transcription/turn-boundary messages
        until the connection ends or the loop is stopped. The partial
        transcript accumulator (self._pending_text) lives on the instance,
        not as a local here, so stop_manual_listening() (called from
        outside this closure, via the voice router's HTTP handler) can
        discard it immediately when the user presses Stop mid-utterance."""

        # Physical-latency instrumentation for one turn. A dict (not a pile
        # of `nonlocal` floats) so any nested function below can record a
        # stage by mutating it in place -- no `nonlocal` declarations to
        # keep in sync, and _finish_turn can log+reset every field in one
        # place. Keys: speech_start, speech_end, transcript_final,
        # route_start (== when execute_text is invoked), route_end (== when
        # it returns), route (the resolved intent, for the log line), tts_start
        # (first synthesized chunk enqueued), tts_end (playback confirmed done).
        timing: dict[str, float | str | None] = {}
        # Per-turn ElevenLabs state, reset alongside `timing`. Once
        # ElevenLabs fails once in a turn, later sentences in that same
        # response go straight to FAST_TTS/Kokoro rather than retrying --
        # PART I of the integration spec this was built against is explicit
        # that a flaky ElevenLabs stream must not make the user wait
        # through repeated retries within one response.
        tts_state: dict[str, bool] = {"elevenlabs_failed": False}

        def _reset_timing() -> None:
            timing.clear()
            tts_state["elevenlabs_failed"] = False

        def _t(key: str) -> float | None:
            value = timing.get(key)
            return value if isinstance(value, float) else None

        async def send_mic_audio() -> None:
            from voice.audio_utils import resample_pcm16

            while self._running.is_set():
                raw = await mic_capture.chunks.get()
                # THE mic-forwarding gate, enforced at the actual send
                # boundary -- mic bytes are captured (drained here) but
                # never forwarded to Gemini, dropped immediately, never
                # buffered for later, whenever ANY of:
                #  - manual listening is off (see start/stop_manual_listening
                #    -- this is what makes "manual_listening_enabled=false"
                #    genuinely stop TARS from hearing anything, not just
                #    stop it from acting on what it hears)
                #  - TARS is executing a turn or speaking (plus its
                #    cooldown) -- half-duplex: this is what stops TARS's
                #    own speaker output -- physically picked up by the mic,
                #    since this is a plain desktop setup with no acoustic
                #    echo cancellation -- from ever being transcribed and
                #    re-executed as a new command, and what stops a second
                #    spoken utterance from ever queuing up behind the first.
                if (
                    not self._manual_listening_enabled
                    or self._playback_active
                    or self._turn_phase != "listening"
                ):
                    self.status.mic_streaming = False
                    continue
                pcm16_native = _downmix_to_pcm16(raw)
                pcm16_target = resample_pcm16(
                    pcm16_native, mic_capture.sample_rate, INPUT_SAMPLE_RATE
                )
                try:
                    await session.send_realtime_input(
                        audio=types.Blob(
                            data=pcm16_target, mime_type=f"audio/pcm;rate={INPUT_SAMPLE_RATE}"
                        )
                    )
                except Exception as exc:
                    self.status.mic_streaming = False
                    self.status.last_error = f"{_now_iso()} send_realtime_input: {exc}"
                    raise
                self.status.mic_streaming = True
                self.status.last_audio_sent_at = _now_iso()

        async def _finish_turn() -> None:
            """Turn-local cleanup only -- conversation history/memory/
            session context are untouched (they live in the database via
            execute_text, not here). Always the last thing any turn does,
            success or failure, spoken or silent. Only after this runs does
            turn_phase return to "listening", so the status pill only ever
            shows Listening (or Voice Off, if manual listening was turned
            off mid-turn) when TARS is genuinely ready for a new
            utterance."""
            active_turn_id = self._active_turn_id
            self._turn_phase = "listening"
            self.status.state = self._idle_status_label()
            self._playback_active = False

            if self._elevenlabs_tts is not None:
                try:
                    await self._elevenlabs_tts.end_turn()
                except Exception:
                    logger.warning(
                        "GeminiLiveLoop: turn_id=%s failed to close the ElevenLabs "
                        "connection cleanly",
                        active_turn_id,
                        exc_info=True,
                    )

            now = time.monotonic()
            logger.info(
                "GeminiLiveLoop: turn_id=%s route=%s LATENCY_MS "
                "SPEECH_TO_TRANSCRIPT=%s TRANSCRIPT_TO_ROUTE=%s "
                "ROUTE_TO_RESPONSE=%s RESPONSE_TO_AUDIO=%s TOTAL=%s "
                "ELEVENLABS_CONNECT_MS=%s FIRST_TEXT_TO_AUDIO_MS=%s "
                "TOTAL_SPEECH_START_MS=%s",
                active_turn_id,
                timing.get("route") or "n/a",
                _ms(_t("transcript_final"), _t("speech_end")),
                _ms(_t("route_start"), _t("transcript_final")),
                _ms(_t("route_end"), _t("route_start")),
                _ms(_t("tts_start"), _t("route_end")),
                _ms(_t("tts_end") or now, _t("speech_start") or _t("speech_end")),
                (
                    f"{timing['elevenlabs_connect_ms']:.1f}"
                    if isinstance(timing.get("elevenlabs_connect_ms"), float)
                    else "n/a"
                ),
                _ms(_t("tts_start"), _t("first_delta")),
                _ms(_t("tts_start"), _t("transcript_final")),
            )

            self._pending_text = ""
            _reset_timing()
            self._active_turn_id = None

            # Explicit, auditable flush -- send_mic_audio already drains
            # (and, while gated, discards) mic_capture.chunks continuously,
            # so this should normally find nothing; it exists as the
            # literal, provable guarantee that zero audio captured during
            # this turn can ever be forwarded after this point.
            drained = 0
            while not mic_capture.chunks.empty():
                try:
                    mic_capture.chunks.get_nowait()
                    drained += 1
                except asyncio.QueueEmpty:
                    break
            if drained:
                logger.info(
                    "GeminiLiveLoop: turn_id=%s flushed %d stale mic chunk(s) before "
                    "resuming listening",
                    active_turn_id,
                    drained,
                )

        def _mark_speaking(provider_name: str) -> None:
            if _t("tts_start") is None:
                self._playback_active = True
                self._turn_phase = "speaking"
                self.status.state = "speaking"
                timing["tts_start"] = time.monotonic()
                self.status.last_audio_response_at = _now_iso()
            self.status.tts_provider = provider_name

        async def _speak_chunk(chunk_text: str, turn_id: str) -> None:
            """Synthesize and enqueue ONE sentence's audio, pipelined with
            generation -- the first chunk written flips TARS into
            "speaking" state; playback.write() never blocks, so synthesis
            of the NEXT sentence can start immediately while this one is
            already playing. May be called multiple times per turn.

            ElevenLabs streaming is tried first (see
            voice/providers/elevenlabs_tts.py): each audio chunk it returns
            is written to the speaker the instant it arrives, real
            incremental streaming rather than buffer-then-play. Falls back
            to FAST_TTS/Kokoro (_synthesize_chunk) only if ElevenLabs
            produced zero audio for this sentence -- if it produced some
            audio and then failed mid-stream, that sentence is treated as
            already (partially) spoken rather than risking speaking it
            twice through a second provider."""
            if self._active_turn_id != turn_id:
                return  # superseded by a newer turn -- never speak stale audio

            if self._elevenlabs_tts is not None and not tts_state["elevenlabs_failed"]:
                spoke_any = False
                try:
                    async for pcm in self._elevenlabs_tts.stream_chunk(chunk_text):
                        if self._active_turn_id != turn_id:
                            return  # superseded mid-stream -- discard the rest
                        if not spoke_any and self._elevenlabs_tts.last_connect_ms is not None:
                            timing["elevenlabs_connect_ms"] = self._elevenlabs_tts.last_connect_ms
                        _mark_speaking("elevenlabs")
                        spoke_any = True
                        playback.write(pcm)
                    if spoke_any:
                        return
                except VoiceProviderError as exc:
                    # The WebSocket handshake can succeed and then the
                    # server can still reject the request (e.g. an invalid
                    # voice_id) before any audio is ever sent -- record the
                    # connect time whenever one actually happened, not only
                    # on the success path, so ELEVENLABS_CONNECT_MS reflects
                    # reality even on a same-turn fallback.
                    if (
                        "elevenlabs_connect_ms" not in timing
                        and self._elevenlabs_tts.last_connect_ms is not None
                    ):
                        timing["elevenlabs_connect_ms"] = self._elevenlabs_tts.last_connect_ms
                    logger.warning(
                        "GeminiLiveLoop: turn_id=%s ElevenLabs streaming failed, "
                        "falling back to SAPI/Kokoro for the rest of this "
                        "response: %s",
                        turn_id,
                        exc,
                    )
                    tts_state["elevenlabs_failed"] = True
                    if spoke_any:
                        return

            from voice.audio_utils import resample_pcm16, wav_to_pcm16

            result, provider_name = await self._synthesize_chunk(chunk_text)
            if result is None:
                return
            pcm, sample_rate = wav_to_pcm16(result.audio)
            if sample_rate != OUTPUT_SAMPLE_RATE:
                pcm = resample_pcm16(pcm, sample_rate, OUTPUT_SAMPLE_RATE)
            _mark_speaking(provider_name or "sapi")
            playback.write(pcm)

        async def _finish_speaking() -> None:
            """Waits for whatever was queued by _speak_chunk to actually
            finish playing (a no-op if nothing was ever spoken this turn),
            then the half-duplex cooldown, then hands off to _finish_turn."""
            if _t("tts_start") is not None:
                try:
                    await asyncio.wait_for(
                        playback.wait_for_drain(), timeout=PLAYBACK_TIMEOUT_SECONDS
                    )
                except TimeoutError:
                    logger.warning(
                        "GeminiLiveLoop: playback did not confirm completion "
                        "within %.0fs",
                        PLAYBACK_TIMEOUT_SECONDS,
                    )
                except Exception as exc:
                    logger.exception("GeminiLiveLoop: playback failed")
                    self.status.last_error = f"{_now_iso()} playback: {exc}"
                timing["tts_end"] = time.monotonic()
                # Half-duplex cooldown: playback_active (checked by
                # send_mic_audio) stays true through this wait -- only
                # cleared by _finish_turn below -- so the mic stays
                # withheld through any acoustic tail of TARS's own speaker
                # output.
                await asyncio.sleep(POST_SPEECH_COOLDOWN_SECONDS)
            await _finish_turn()

        async def handle_user_utterance(
            turn_id: str, user_text: str, created_monotonic: float
        ) -> None:
            # Staleness backstop (see STALE_TURN_MAX_AGE_SECONDS): should be
            # a no-op today since this task is created in the same
            # event-loop tick the transcript finalized, but this is the
            # explicit, provable guarantee that an old transcript can never
            # execute late, rather than an assumption that scheduling is
            # always instant.
            age_seconds = time.monotonic() - created_monotonic
            if age_seconds > STALE_TURN_MAX_AGE_SECONDS:
                logger.warning(
                    "GeminiLiveLoop: DROPPED_STALE_TURN turn_id=%s age_ms=%.0f",
                    turn_id,
                    age_seconds * 1000,
                )
                if self._active_turn_id == turn_id:
                    await _finish_turn()
                return

            wake_match = self._wake_matcher.match(user_text)
            # Manual listening mode is itself permission to accept speech
            # with no wake phrase, same as an open follow-up window --
            # pressing Start Listening means every utterance while it's on
            # executes directly, wake phrase or not.
            follow_up_active = self._manual_listening_enabled or (
                self._follow_up_window_until is not None
                and time.monotonic() < self._follow_up_window_until
            )
            command_text = _resolve_command(user_text, wake_match, follow_up_active=follow_up_active)
            if command_text is None:
                # No wake phrase and no open follow-up window -- ambient
                # room speech (TV, another conversation, background
                # noise). Drop it outright: no provider call, no TTS, no
                # turn ever recorded. This is the primary fix for ambient
                # audio creating spurious TARS turns.
                logger.info(
                    "GeminiLiveLoop: turn_id=%s DROPPED_NO_WAKE (no wake phrase, "
                    "follow-up window closed): %r",
                    turn_id,
                    user_text,
                )
                self.status.last_dropped_no_wake_transcript = user_text
                await _finish_turn()
                return

            if _looks_like_echo(command_text, self.status.last_tars_response):
                logger.warning(
                    "GeminiLiveLoop: turn_id=%s discarding likely echo of TARS's "
                    "own last response: %r",
                    turn_id,
                    user_text,
                )
                await _finish_turn()
                return

            logger.info(
                "GeminiLiveLoop: turn_id=%s recognized utterance: %r "
                "(wake_prefix_stripped=%s, routed as: %r)",
                turn_id,
                user_text,
                wake_match is not None,
                command_text,
            )
            self.status.last_turn_received_at = _now_iso()
            timing["route_start"] = time.monotonic()

            accumulated = ""
            spoken_count = 0
            final_response = None
            try:
                async for event in self._stream_response(command_text, turn_id):
                    if event.type == "delta" and event.text:
                        if "first_delta" not in timing:
                            timing["first_delta"] = time.monotonic()
                        accumulated += event.text
                        chunks = sentence_chunks(accumulated)
                        new_chunks = _pending_speakable_chunks(chunks, spoken_count, is_final=False)
                        for chunk in new_chunks:
                            await _speak_chunk(chunk, turn_id)
                        spoken_count += len(new_chunks)
                    elif event.type == "complete":
                        final_response = event.response
            except Exception as exc:
                logger.exception("GeminiLiveLoop: turn_id=%s TARS execution failed", turn_id)
                self.status.last_error = f"{_now_iso()} stream_text: {exc}"

            timing["route_end"] = time.monotonic()

            if self._active_turn_id != turn_id:
                # Superseded by a newer turn while the stream was running
                # (should not happen given strict turn serialization, but
                # this is the explicit "never deliver a stale queued
                # response" guard) -- drop it rather than speak it.
                logger.warning(
                    "GeminiLiveLoop: turn_id=%s response arrived after being superseded "
                    "by turn_id=%s -- discarding rather than speaking a stale response",
                    turn_id,
                    self._active_turn_id,
                )
                return

            if final_response is not None:
                if final_response.status is TurnStatus.COMPLETED:
                    # Open/refresh the follow-up window: "Explain that more
                    # simply" right after a real answer shouldn't need the
                    # wake phrase again. A failed/ignored turn does not
                    # extend it -- an error response is not an invitation
                    # to keep listening without a wake phrase.
                    self._follow_up_window_until = time.monotonic() + FOLLOW_UP_WINDOW_SECONDS
                self.status.wake_required = not (
                    self._follow_up_window_until is not None
                    and time.monotonic() < self._follow_up_window_until
                )
                timing["route"] = (
                    final_response.intent.value
                    if hasattr(final_response.intent, "value")
                    else str(final_response.intent)
                )
                speech_text = final_response.speech_text.strip()
                logger.info(
                    "GeminiLiveLoop: turn_id=%s TARS response status=%s intent=%s "
                    "provider=%s route_latency_ms=%.1f speech_text=%r",
                    turn_id,
                    final_response.status,
                    final_response.intent,
                    final_response.provider,
                    final_response.latency_ms,
                    speech_text,
                )
                self.status.last_tars_response = speech_text
                # Catch-up: covers both the trailing sentence fragment of a
                # streamed reply (withheld by _pending_speakable_chunks
                # until final) and non-streaming routes (deterministic/
                # action), whose entire answer only ever arrives in this
                # one final event with no prior deltas at all.
                if "first_delta" not in timing:
                    timing["first_delta"] = time.monotonic()
                final_chunks = sentence_chunks(speech_text)
                new_chunks = _pending_speakable_chunks(final_chunks, spoken_count, is_final=True)
                for chunk in new_chunks:
                    await _speak_chunk(chunk, turn_id)
                spoken_count += len(new_chunks)
            elif spoken_count == 0:
                fallback = "Sorry, something went wrong handling that."
                self.status.last_tars_response = fallback
                await _speak_chunk(fallback, turn_id)

            await _finish_speaking()

        send_task = asyncio.create_task(send_mic_audio())
        try:
            async for message in session.receive():
                if not self._running.is_set():
                    break

                if message.session_resumption_update and message.session_resumption_update.new_handle:
                    self._resumption_handle = message.session_resumption_update.new_handle

                if message.go_away:
                    logger.info(
                        "GeminiLiveLoop: server signaled go_away (time_left=%s), will reconnect",
                        message.go_away.time_left,
                    )

                # Real server-side VAD signal (not guessed/timed locally) --
                # drives the UI's distinct "hearing you" state, only
                # meaningful while actually waiting on the user (not mid
                # processing/speaking) AND while manual listening is on --
                # with it off no audio is ever sent (see send_mic_audio),
                # so any VAD event here would only be server-side lag from
                # audio sent just before Stop was pressed.
                if message.voice_activity and message.voice_activity.voice_activity_type:
                    activity = message.voice_activity.voice_activity_type
                    if self._manual_listening_enabled and self._turn_phase == "listening":
                        if activity == types.VoiceActivityType.ACTIVITY_START:
                            self.status.state = "user_speaking"
                            timing["speech_start"] = time.monotonic()
                        elif activity == types.VoiceActivityType.ACTIVITY_END:
                            self.status.state = self._idle_status_label()
                            timing["speech_end"] = time.monotonic()

                content = message.server_content
                if content is None:
                    continue

                if not self._manual_listening_enabled or self._turn_phase != "listening":
                    # Either manual listening is off -- no permission to
                    # accept speech, so even a lingering transcript
                    # fragment from audio sent just before Stop was
                    # pressed must be discarded, never turned into a
                    # command -- or MAX_ACTIVE_USER_TURNS == 1 and TARS is
                    # already executing/speaking a previous turn. Gemini is
                    # ears-only and must never inject a new turn or act on
                    # a spontaneous reply in either case -- discard
                    # everything here unconditionally, never queue it.
                    if content.turn_complete:
                        logger.info(
                            "GeminiLiveLoop: %s turn_complete arrived "
                            "(manual_listening_enabled=%s turn_phase=%s) -- "
                            "ignoring, not queuing",
                            "DROPPED_VOICE_OFF" if not self._manual_listening_enabled else "DROPPED_BUSY",
                            self._manual_listening_enabled,
                            self._turn_phase,
                        )
                    self._pending_text = ""
                    continue

                # Note what is deliberately absent here: content.model_turn
                # (Gemini's own generated audio/text reply) is never read,
                # anywhere in this module. That omission -- not the session
                # config -- is what guarantees the user never hears
                # Gemini's voice; see the module docstring.
                if content.input_transcription and content.input_transcription.text:
                    self._pending_text = content.input_transcription.text
                    self.status.last_input_transcript = self._pending_text
                if content.turn_complete:
                    # Finalize exactly one transcript for this speech
                    # segment, then immediately stop accepting fragments
                    # for it: turn_phase flips to "processing" in the same
                    # tick, so no later message (even one already in
                    # flight from Gemini) can append to `user_text` or spawn
                    # a second turn from the same segment.
                    user_text = self._pending_text.strip()
                    self._pending_text = ""
                    timing["transcript_final"] = time.monotonic()
                    if user_text:
                        self._turn_phase = "processing"
                        self.status.state = "processing"
                        turn_id = uuid.uuid4().hex
                        self._active_turn_id = turn_id
                        asyncio.create_task(
                            handle_user_utterance(turn_id, user_text, time.monotonic())
                        )
                    else:
                        self.status.state = self._idle_status_label()
        finally:
            send_task.cancel()
