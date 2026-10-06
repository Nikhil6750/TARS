"""Local speech-to-text via faster-whisper (CTranslate2-based Whisper
inference) — the default/required SpeechToTextProvider per ADR-011. Model
weights download from Hugging Face on first use per `model_size` and are
cached locally afterward (`~/.cache/huggingface`); no API key, no network
required after that first pull.
"""
from __future__ import annotations

import asyncio
import threading

from voice.errors import VoiceProviderError
from voice.interfaces import SpeechToTextProvider, TranscriptionResult
from voice.stt_runtime import (
    STTHealth,
    STTState,
    detect_backend,
    register_cuda_dll_dirs,
    normalize_stt_text,
    resolve_model_dir,
    resolve_model_name,
)

PCM16_SCALE = 32768.0


_HOTWORDS = {"tars", "eurusd", "xauusd", "gold"}
# Decoding bias only (never a transcript substitution): TARS-specific vocabulary.
VOCAB_HOTWORDS = ("TARS, TradingView, XAUUSD, EURUSD, GBPUSD, BTCUSD, gold, Bitcoin, timeframe, "
                  "Claude, Gemini, Calculator, PowerShell, Chrome")


def _is_hotword_echo(text: str) -> bool:
    words = [w.strip(".,!?").lower() for w in text.split()]
    return bool(words) and all(w in _HOTWORDS for w in words) and len(words) >= 3


def np_silence():
    import numpy as np

    return np.zeros(16000, dtype=np.float32)


class FasterWhisperSTTProvider(SpeechToTextProvider):
    name = "faster_whisper"
    sample_rate = 16000

    def __init__(self, model_size: str = "small.en", device: str = "auto", compute_type: str = "auto",
                 *, model_dir: str | None = None, beam_size: int = 1, model_factory=None):
        self._inference_lock = threading.Lock()
        self.beam_size = max(1, int(beam_size))
        self.model_size = resolve_model_name(model_size)
        self.health = STTHealth(model=self.model_size, state=STTState.LOADING)
        self.model_dir = resolve_model_dir(model_dir)
        self.health.model_dir = str(self.model_dir)
        if model_factory is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                self.health.state, self.health.detail = STTState.ERROR, "faster-whisper is not installed"
                raise VoiceProviderError(
                    "faster-whisper is not installed — pip install -r requirements-voice.txt"
                ) from exc
            model_factory = WhisperModel

        register_cuda_dll_dirs()
        resolved_device, resolved_compute = detect_backend("cpu" if device == "cpu" else "auto")
        if compute_type not in ("auto", "") and resolved_device == "cpu":
            resolved_compute = compute_type
        attempts = [(resolved_device, resolved_compute)]
        if resolved_device != "cpu":
            attempts.append(("cpu", "int8"))  # graceful CPU fallback when CUDA libs fail at load
        last_error: Exception | None = None
        self._model = None
        for dev, comp in attempts:
            try:
                self.model_dir.mkdir(parents=True, exist_ok=True)
                self._model = model_factory(
                    self.model_size, device=dev, compute_type=comp, cpu_threads=4, num_workers=1,
                    download_root=str(self.model_dir),
                )
                if dev == "cuda":
                    # CTranslate2 loads weights fine without cuBLAS/cuDNN and only fails at the first
                    # decode: prove the GPU actually works now, not on the user's first utterance.
                    list(self._model.transcribe(np_silence(), language="en", beam_size=1)[0])
                self.health.backend = "cuda" if dev == "cuda" else "cpu"
                self.health.compute_type = comp
                if last_error is not None:
                    self.health.detail = f"GPU unavailable, using CPU: {type(last_error).__name__}"
                break
            except Exception as exc:  # noqa: BLE001 - reported via health, never fatal to TARS
                last_error = exc
        if self._model is None:
            self.health.state, self.health.detail = STTState.ERROR, f"model load failed: {last_error}"
            raise VoiceProviderError(
                f"failed to load faster-whisper model '{self.model_size}': {last_error}"
            ) from last_error
        self.health.state = STTState.READY

    async def transcribe(self, pcm_audio: bytes) -> TranscriptionResult:
        return await asyncio.to_thread(self._transcribe_sync, pcm_audio)

    def _transcribe_sync(self, pcm_audio: bytes) -> TranscriptionResult:
        import numpy as np

        samples = np.frombuffer(pcm_audio, dtype=np.int16).astype(np.float32) / PCM16_SCALE
        try:
            with self._inference_lock:
                segments, info = self._model.transcribe(
                    samples, language="en", beam_size=self.beam_size, condition_on_previous_text=False,
                    hotwords=VOCAB_HOTWORDS,
                    # Bound decoding: on silence/noise Whisper can hallucinate for 20+ s and hold the
                    # inference lock, stalling every real turn behind it.
                    max_new_tokens=128, temperature=0.0, without_timestamps=True,
                    # Silence/noise must yield nothing, never the hotwords echoed back.
                    vad_filter=True, no_speech_threshold=0.6,
                )
                text = " ".join(segment.text.strip() for segment in segments).strip()
        except Exception as exc:
            raise VoiceProviderError(f"faster-whisper transcription failed: {exc}") from exc
        if _is_hotword_echo(text):
            text = ""
        return TranscriptionResult(text=normalize_stt_text(text), language=info.language if info else None)
