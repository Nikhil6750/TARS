"""Shared audio encoding helpers — kept dependency-free (stdlib `wave` only)
so importing this module never requires numpy/torch/onnxruntime. Adapters
that produce float audio (Kokoro, Fish Speech) convert to PCM16 themselves
via `float32_to_pcm16`, which lazily imports numpy only when called.
"""
from __future__ import annotations

import io
import wave


def pcm16_to_wav(pcm_bytes: bytes, sample_rate: int, channels: int = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)  # 16-bit
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_bytes)
    return buffer.getvalue()


def float32_to_pcm16(samples) -> bytes:
    import numpy as np

    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16).tobytes()


def wav_to_pcm16(wav_bytes: bytes) -> tuple[bytes, int]:
    """Unwraps a WAV file back to raw PCM16 bytes + sample rate — the shape
    Pipecat's `AudioRawFrame` expects (raw PCM, never a WAV container)."""
    buffer = io.BytesIO(wav_bytes)
    with wave.open(buffer, "rb") as wav_file:
        sample_rate = wav_file.getframerate()
        pcm = wav_file.readframes(wav_file.getnframes())
    return pcm, sample_rate
