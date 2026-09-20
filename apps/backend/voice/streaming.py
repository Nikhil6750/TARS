"""Incremental local voice adapters. All buffers and inference queues are bounded.

Whisper is a rolling-window decoder, not a streaming ASR model. We coalesce
partial requests and serialize inference; capture/VAD never wait for decoding.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

from assistant.response_quality import prepare_speech_text
from voice.interfaces import SpeechToTextProvider, SynthesisResult, TextToSpeechProvider

Callback = Callable[[dict], Awaitable[None]]


class StreamingSTTProvider(Protocol):
    async def start(self) -> None: ...
    async def push_audio(self, frame: bytes) -> None: ...
    async def stop(self) -> None: ...
    async def on_speech_started(self, event: dict) -> None: ...
    async def on_partial_transcript(self, event: dict) -> None: ...
    async def on_final_transcript(self, event: dict) -> None: ...
    async def on_speech_ended(self, event: dict) -> None: ...


class StreamingTTSProvider(Protocol):
    async def start(self, text_stream: AsyncIterator[str]) -> None: ...
    def cancel(self) -> None: ...
    def flush(self) -> None: ...
    async def on_audio_chunk(self, audio: SynthesisResult) -> None: ...
    async def on_complete(self) -> None: ...


class SileroStreamingVAD:
    """Use the already installed faster-whisper Silero ONNX weights, with
    per-session recurrent state (never the batch helper that resets each call).
    """

    def __init__(self):
        import numpy as np
        from faster_whisper.vad import get_vad_model
        from numpy.typing import NDArray

        self.session = get_vad_model().session
        self.h: NDArray[np.float32] = np.zeros((1, 1, 128), dtype="float32")
        self.c: NDArray[np.float32] = np.zeros((1, 1, 128), dtype="float32")
        self.context: NDArray[np.float32] = np.zeros((1, 64), dtype="float32")

    def __call__(self, pcm: bytes) -> bool:
        import numpy as np

        samples = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
        audio = np.concatenate((self.context, samples.reshape(1, -1)), axis=1)
        out, self.h, self.c = self.session.run(
            None, {"input": audio, "h": self.h, "c": self.c}
        )
        self.context = samples[-64:].reshape(1, -1)
        return float(out.reshape(-1)[-1]) >= 0.6


class SherpaPartialEngine:
    """True streaming partial transcripts (sherpa-onnx zipformer, ~2 ms/32 ms chunk).

    Only drives live partials + endpoint stability; the accurate final transcript
    still comes from faster-whisper over the finished utterance. Optional: absent
    model/package simply means IncrementalWhisperSTT uses whisper rolling partials.
    """

    _recognizer = None

    @classmethod
    def available(cls, model_dir: str) -> bool:
        from pathlib import Path
        try:
            import sherpa_onnx  # noqa: F401
        except ImportError:
            return False
        return (Path(model_dir) / "tokens.txt").exists()

    def __init__(self, model_dir: str):
        import sherpa_onnx
        from pathlib import Path

        if SherpaPartialEngine._recognizer is None:
            d = Path(model_dir)
            SherpaPartialEngine._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=str(d / "tokens.txt"),
                encoder=str(d / "encoder-epoch-99-avg-1.int8.onnx"),
                decoder=str(d / "decoder-epoch-99-avg-1.int8.onnx"),
                joiner=str(d / "joiner-epoch-99-avg-1.int8.onnx"),
                num_threads=2, sample_rate=16000)
        self.r = SherpaPartialEngine._recognizer
        self.stream = None

    def reset(self):
        self.stream = self.r.create_stream()

    def feed(self, pcm: bytes) -> str:
        import numpy as np

        if self.stream is None:
            self.reset()
        self.stream.accept_waveform(16000, np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0)
        while self.r.is_ready(self.stream):
            self.r.decode_stream(self.stream)
        result = self.r.get_result(self.stream)
        return (result if isinstance(result, str) else result.text).strip().lower().capitalize()


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def is_echo(candidate: str, reference: str) -> bool:
    """True when what the mic heard is (mostly) TARS's own current speech."""
    heard, spoken = _words(candidate), set(_words(reference))
    if not heard or not spoken:
        return False
    return sum(w in spoken for w in heard) / len(heard) >= 0.6


class IncrementalWhisperSTT:
    name = "incremental_faster_whisper"

    def __init__(self, provider: SpeechToTextProvider, emit: Callback,
                 vad: Callable[[bytes], bool], *, partial_interval: float = 0.48,
                 partial_engine: "SherpaPartialEngine | None" = None,
                 busy: Callable[[], bool] | None = None,
                 echo_reference: Callable[[], str] | None = None):
        self.busy, self.echo_reference = busy, echo_reference
        self.announced = True
        self.valid_utterance = 0
        self.provider, self.emit, self.vad = provider, emit, vad
        self.partial_interval = partial_interval
        self.partial_engine = partial_engine
        self.text_changed_at = 0.0
        self.pending = bytearray()
        self.preroll: deque[bytes] = deque(maxlen=6)
        self.audio = bytearray()
        self.active = False
        self.utterance = 0
        self.silence = 0.0
        self.duration = 0.0
        self.next_partial = partial_interval
        self.last_text = ""
        self.stable = 0
        self.speech_frames = 0
        self.voiced = 0.0
        self._requests: deque[tuple[int, bytes, bool]] = deque(maxlen=2)
        self._wake = asyncio.Event()
        self._worker: asyncio.Task | None = None

    async def on_speech_started(self, event: dict) -> None:
        await self.emit({"type": "speech_started", **event})

    async def on_partial_transcript(self, event: dict) -> None:
        await self.emit({"type": "partial_transcript", **event})

    async def on_final_transcript(self, event: dict) -> None:
        await self.emit({"type": "final_transcript", **event})

    async def on_speech_ended(self, event: dict) -> None:
        await self.emit({"type": "speech_ended", **event})

    async def start(self) -> None:
        self._worker = asyncio.create_task(self._decode())

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        self._requests.clear()
        self.pending.clear()
        self.audio.clear()

    async def push_audio(self, frame: bytes) -> None:
        if len(frame) > 32000 or len(frame) % 2:
            raise ValueError("expected at most one second of PCM16 mono at 16000 Hz")
        self.pending.extend(frame)
        while len(self.pending) >= 1024:
            chunk = bytes(self.pending[:1024])
            del self.pending[:1024]
            speech = self.vad(chunk)
            self.preroll.append(chunk)
            self.speech_frames = self.speech_frames + 1 if speech else 0
            if not self.active:
                # 192ms confirmation: room noise / echo blips must not steal a turn or
                # interrupt the assistant.
                if self.speech_frames < 6:
                    continue
                self.utterance += 1
                self.active = True
                self.audio = bytearray(b"".join(self.preroll))
                self.duration, self.silence = 0.0, 0.0
                self.voiced = 0.0
                self.last_text, self.stable = "", 0
                self.next_partial = self.partial_interval
                self.text_changed_at = 0.0
                # While TARS speaks/thinks, the mic mostly hears TARS itself (no AEC). A barge-in is
                # only announced once recognised words are NOT an echo of TARS's own speech.
                self.announced = not (self.partial_engine or (self.busy and self.busy()))
                if self.announced:
                    await self.on_speech_started({"utterance": self.utterance})
                if self.partial_engine:
                    self.partial_engine.reset()
                    await self._engine_feed(b"".join(self.preroll))
            else:
                self.audio.extend(chunk)
                if self.partial_engine:
                    await self._engine_feed(chunk)
            self.duration += 0.032
            self.silence = 0.0 if speech else self.silence + 0.032
            if speech:
                self.voiced += 0.032
            if self.active and not self.announced and not self.partial_engine and self.voiced >= 0.8:
                self.announced = True
                await self.on_speech_started({"utterance": self.utterance})
            if self.partial_engine:
                # Stable = unchanged for 250 ms of audio (streaming text updates per chunk).
                self.stable = 1 if self.duration - self.text_changed_at >= 0.25 and self.last_text else 0
            elif self.duration >= self.next_partial and speech:
                self._request(final=False)
                self.next_partial = self.duration + self.partial_interval
            # Stable complete phrases can endpoint quickly. Short/unstable phrases
            # get more breathing room. The final decode includes the last words.
            endpoint = 0.384 if self.stable >= 1 and len(self.last_text.split()) >= 3 else 0.736
            if self.silence >= 0.096:
                await self.emit({"type": "endpointing", "utterance": self.utterance})
            elif speech:
                await self.emit({"type": "speech_resumed", "utterance": self.utterance})
            if self.silence >= endpoint or self.duration >= 20.0:
                self.active = False
                # Noise gate: too little voiced audio (or nothing recognisable by the streaming
                # engine on a short blip) is not a turn. Skipping the final decode also keeps
                # 1s+ whisper decodes from queueing behind noise.
                if not self.announced and not self.partial_engine and self.voiced >= 0.8:
                    # no streaming engine to vet the words: a long-enough utterance is a barge-in
                    self.announced = True
                    await self.on_speech_started({"utterance": self.utterance})
                if not self.announced:
                    await self.emit({"type": "speech_discarded", "utterance": self.utterance, "silent": True})
                elif self.voiced < 0.4 or (self.partial_engine and not self.last_text and self.voiced < 1.0):
                    await self.emit({"type": "speech_discarded", "utterance": self.utterance})
                else:
                    await self.on_speech_ended({"utterance": self.utterance,
                                               "at": time.perf_counter() - self.silence})
                    self.valid_utterance = self.utterance
                    self._request(final=True)
                self.audio.clear()
                self.preroll.clear()

    async def _engine_feed(self, pcm: bytes):
        try:
            text = self.partial_engine.feed(pcm)
        except Exception as exc:
            await self.emit({"type": "provider_error", "provider": "stt",
                             "detail": type(exc).__name__, "utterance": self.utterance})
            self.partial_engine = None
            return
        if text and text != self.last_text:
            self.last_text, self.text_changed_at = text, self.duration
            if not self.announced:
                # Words recognised by the streaming engine are what make noise a turn. While TARS
                # itself is talking, they must also not be an echo of TARS's own speech.
                busy = bool(self.busy and self.busy())
                reference = self.echo_reference() if (busy and self.echo_reference) else ""
                if len(_words(text)) >= (2 if busy else 1) and not is_echo(text, reference):
                    self.announced = True
                    await self.on_speech_started({"utterance": self.utterance})
                else:
                    return
            await self.on_partial_transcript({"utterance": self.utterance, "text": text})

    def _request(self, *, final: bool):
        # Coalesce queued partials; never accumulate stale inference work.
        self._requests = deque((r for r in self._requests if r[2]), maxlen=2)
        self._requests.append((self.utterance, bytes(self.audio), final))
        self._wake.set()

    async def _decode(self):
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._requests:
                utterance, pcm, final = self._requests.popleft()
                if utterance != (self.valid_utterance if final else self.utterance):
                    continue  # superseded before we started decoding
                try:
                    result = await asyncio.wait_for(self.provider.transcribe(pcm), 15)
                    if utterance != (self.valid_utterance if final else self.utterance):
                        continue
                    text = result.text.strip()
                    if not final and not self.active:
                        continue
                    if not final:
                        self.stable = self.stable + 1 if text == self.last_text and text else 0
                        self.last_text = text
                    callback = self.on_final_transcript if final else self.on_partial_transcript
                    await callback({"utterance": utterance, "text": text})
                except Exception as exc:
                    await self.emit({"type": "provider_error", "provider": "stt",
                                     "detail": type(exc).__name__, "utterance": utterance})


class SpeechChunker:
    """Hold incomplete syntax/numbers. Only finished sentences or clauses speak.
    Fence and inline-code contents are withheld even across token boundaries.
    """

    def __init__(self):
        self.buffer = ""

    def feed(self, text: str, *, final: bool = False) -> list[str]:
        self.buffer += text
        chunks = []
        while self.buffer:
            # Wait for complete code fences before considering punctuation inside.
            clean = re.sub(r"```[\s\S]*?```", " ", self.buffer)
            clean = re.sub(r"<(tool_use|tool_result|thinking)>[\s\S]*?</\1>", " ", clean)
            if re.search(r"<(?:tool_use|tool_result|thinking)>", clean):
                if not final:
                    break
                clean = re.split(r"<(?:tool_use|tool_result|thinking)>", clean)[0]
            if "```" in clean or clean.count("`") % 2:
                if not final:
                    break
                clean = clean.split("```")[0].split("`")[0]
            clean = re.sub(r"`[^`]*`", "", clean)
            boundary = re.search(r"[.!?;](?=\s)|,(?=\s)", clean)
            if boundary is None and not final:
                break
            end = boundary.end() if boundary else len(clean)
            # Clean is now our canonical buffer; removed code must never return.
            self.buffer = clean[end:]
            unit = clean[:end]
            unit = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", unit)
            unit = re.sub(r"https?://\S+|www\.\S+", "", unit)
            unit = re.sub(r"(?im)^.*(?:tool_use|tool_result|<tool|\[tool|internal trace).*$", "", unit)
            unit = prepare_speech_text(unit)
            if unit:
                chunks.append(unit)
            if not clean:
                self.buffer = ""
        return chunks


class LocalStreamingTTS:
    """Sentence/clause streaming over the installed local provider.

    Cancel invalidates results before cancelling the await: an in-flight ONNX
    kernel cannot be preempted, but it can never enqueue audio after cancellation.
    No detached kokoro.create_stream producer is used (it is uncancellable).
    """

    def __init__(self, provider: TextToSpeechProvider,
                 on_audio_chunk: Callable[[SynthesisResult], Awaitable[None]],
                 on_complete: Callable[[], Awaitable[None]]):
        self.provider = provider
        self.on_audio_chunk, self.on_complete = on_audio_chunk, on_complete
        self.generation = 0
        self.task: asyncio.Task | None = None

    async def start(self, text_stream: AsyncIterator[str]) -> None:
        generation = self.generation
        self.task = asyncio.current_task()
        async for text in text_stream:
            if generation != self.generation:
                return
            stream = getattr(self.provider, "synthesize_stream", None)
            if callable(stream):
                iterator = stream(text)
                try:
                    async with asyncio.timeout(30):
                        async for result in iterator:
                            if generation != self.generation:
                                return
                            await self.on_audio_chunk(result)
                finally:
                    await iterator.aclose()
            else:
                result = await asyncio.wait_for(self.provider.synthesize(text), 20)
                if generation != self.generation:
                    return
                await self.on_audio_chunk(result)
        if generation == self.generation:
            await self.on_complete()

    def cancel(self) -> None:
        self.generation += 1
        if self.task and self.task is not asyncio.current_task():
            self.task.cancel()

    def flush(self) -> None:
        self.cancel()
