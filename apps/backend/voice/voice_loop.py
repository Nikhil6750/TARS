"""Backend-owned continuous microphone loop -- the single production
automatic-listening owner.

Captures via the proven WASAPI Float32 path (`Float32Microphone` below):
this hardware's WASAPI mix format is float32, and requesting int16 directly
from PortAudio hits a lossy conversion path (physically measured peak
~0.0006 vs ~0.08 for the same speech at float32 -- see
`scripts/_scratch_float32_capture.py` and the golden-loop root-cause
investigation). `speech_recognition.Microphone` hardcodes int16 internally
and cannot be reconfigured, so `Float32Microphone` is a small
`AudioSource`-compatible shim: it captures float32 stereo from PortAudio,
downmixes and converts to correctly-scaled int16 *before* handing bytes to
the Recognizer, so its battle-tested ambient-noise calibration and phrase
boundary detection (`audioop`-based) operate on real data.

Every recognized utterance is resampled once (the same `resample_pcm16`
production utility used by the native HTTP path) and executed through the
one canonical `AssistantTurnController.execute_utterance` -- in-process,
not over HTTP, since this loop runs inside the same backend process. No
separate command dispatch, no second turn owner.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Any

import numpy as np
import pyaudio
import speech_recognition as sr

logger = logging.getLogger("tars.voice.voice_loop")


class _FloatToInt16Stream:
    """Wraps a PyAudio paFloat32 stereo stream. `.read(n)` returns `n`
    frames of correctly-scaled mono PCM16 bytes -- what
    `speech_recognition.Recognizer` (via `audioop`) expects."""

    def __init__(self, pyaudio_stream) -> None:
        self._stream = pyaudio_stream

    def read(self, chunk_frames: int) -> bytes:
        raw = self._stream.read(chunk_frames, exception_on_overflow=False)
        stereo = np.frombuffer(raw, dtype="<f4").reshape(-1, 2)
        mono = (stereo[:, 0] + stereo[:, 1]) / 2.0
        pcm16 = (np.clip(mono, -1.0, 1.0) * 32767.0).astype("<i2")
        return pcm16.tobytes()

    def close(self) -> None:
        self._stream.stop_stream()
        self._stream.close()


class Float32Microphone(sr.AudioSource):
    """`speech_recognition.AudioSource` implementation using the proven
    WASAPI float32 capture path instead of `sr.Microphone`'s hardcoded
    (and, on this hardware, lossy) int16 request. Does not call
    `AudioSource.__init__` -- it unconditionally raises, since the base
    class is an abstract marker with no real state."""

    def __init__(self, *, device_index: int, sample_rate: int, chunk_size: int = 1024) -> None:
        self.pyaudio_module = pyaudio
        self.device_index = device_index
        self.format = pyaudio.paInt16  # presented format, for audioop compatibility
        self.SAMPLE_WIDTH = 2
        self.SAMPLE_RATE = sample_rate
        self.CHUNK = chunk_size
        self.audio: Any = None
        self.stream: _FloatToInt16Stream | None = None

    def __enter__(self) -> Float32Microphone:
        self.audio = self.pyaudio_module.PyAudio()
        raw_stream = self.audio.open(
            input_device_index=self.device_index,
            channels=2,
            format=self.pyaudio_module.paFloat32,
            rate=self.SAMPLE_RATE,
            frames_per_buffer=self.CHUNK,
            input=True,
        )
        self.stream = _FloatToInt16Stream(raw_stream)
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            if self.stream is not None:
                self.stream.close()
        finally:
            if self.audio is not None:
                self.audio.terminate()
            self.stream = None
            self.audio = None


@dataclass
class VoiceLoopStatus:
    """Observed by the frontend (via a status endpoint) -- the UI must
    reflect this, not invent its own listening state."""

    state: str = "stopped"  # stopped|starting|listening|recognizing|thinking|speaking
    device_name: str | None = None
    error: str | None = None


class VoiceLoop:
    """Owns continuous microphone capture end-to-end. One instance per
    backend process, started once at app startup when real voice providers
    are configured."""

    def __init__(self, *, settings, controller, event_loop: asyncio.AbstractEventLoop) -> None:
        self._settings = settings
        self._controller = controller
        self._event_loop = event_loop
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self.status = VoiceLoopStatus()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._running.set()
        self._thread = threading.Thread(target=self._run, name="tars-voice-loop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()

    def _run(self) -> None:
        from voice.mic_device import resolve_default_input_device

        self.status.state = "starting"
        try:
            mic_info = resolve_default_input_device()
        except Exception as exc:
            logger.exception("VoiceLoop: could not resolve a microphone device")
            self.status.state = "stopped"
            self.status.error = str(exc)
            return

        self.status.device_name = mic_info.name
        recognizer = sr.Recognizer()
        # Calibrate once at startup and never again. Left at its default
        # (True), dynamic_energy_threshold continuously readjusts the
        # trigger threshold during every listen() call; physically that
        # produced wildly different calibrated thresholds run to run
        # (20.9/57.9/128.6/290.7) and repeated false triggers on small
        # ambient sounds as the loop ran longer. A single fixed threshold,
        # padded for headroom, is deliberately more conservative.
        recognizer.dynamic_energy_threshold = False
        recognizer.pause_threshold = 0.9
        recognizer.phrase_threshold = 0.25
        recognizer.non_speaking_duration = 0.5
        microphone = Float32Microphone(
            device_index=mic_info.device_index, sample_rate=mic_info.sample_rate
        )

        try:
            with microphone as source:
                recognizer.adjust_for_ambient_noise(source, duration=1.0)
                recognizer.energy_threshold *= 1.5  # conservative headroom, fixed from here on
        except Exception as exc:
            logger.exception("VoiceLoop: ambient noise calibration failed")
            self.status.state = "stopped"
            self.status.error = str(exc)
            return

        logger.info(
            "VOICE LOOP STARTED device=%s mode=%s stt=%s sample_rate=16000 "
            "energy_threshold=%.1f (fixed) pause_threshold=%.2f phrase_threshold=%.2f "
            "non_speaking_duration=%.2f",
            mic_info.name,
            self._settings.voice_mode,
            self._settings.faster_whisper_model,
            recognizer.energy_threshold,
            recognizer.pause_threshold,
            recognizer.phrase_threshold,
            recognizer.non_speaking_duration,
        )

        while self._running.is_set():
            self.status.state = "listening"
            try:
                with microphone as source:
                    audio = recognizer.listen(source, timeout=1, phrase_time_limit=6)
            except sr.WaitTimeoutError:
                continue
            except Exception:
                logger.exception("VoiceLoop: microphone read failed, retrying")
                self.status.error = "microphone read failed"
                continue

            if not self._running.is_set():
                break

            self.status.state = "recognizing"
            future = asyncio.run_coroutine_threadsafe(
                self._handle_utterance(audio), self._event_loop
            )
            try:
                future.result()
            except Exception:
                logger.exception("VoiceLoop: turn execution failed")
            if self._running.is_set():
                self.status.state = "listening"

        self.status.state = "stopped"

    async def _handle_utterance(self, audio) -> None:
        from voice.audio_utils import resample_pcm16

        pcm_native = audio.get_raw_data()
        pcm_16k = resample_pcm16(pcm_native, audio.sample_rate, 16000)
        self.status.state = "thinking"
        try:
            response = await self._controller.execute_utterance(
                pcm_16k,
                session_id="backend-voice-loop",
            )
            self.status.state = "speaking" if response.audio_chunks_base64 else "listening"
        except Exception:
            logger.exception("VoiceLoop: turn execution raised")
            self.status.state = "listening"
