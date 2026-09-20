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


def wav_channels(wav_bytes: bytes) -> int:
    """Diagnostic helper: reads the actual channel count from a WAV header."""
    buffer = io.BytesIO(wav_bytes)
    with wave.open(buffer, "rb") as wav_file:
        return wav_file.getnchannels()


def resample_pcm16(pcm_bytes: bytes, source_rate: int, target_rate: int) -> bytes:
    """Resamples a mono PCM16 buffer from `source_rate` to `target_rate`.

    Per the convention documented in `voice/interfaces.py`, every provider
    (`SpeechToTextProvider`, `WakeWordProvider`, ...) declares the exact
    `sample_rate` it expects and never resamples internally — callers must
    hand it audio already at that rate. Captured microphone audio is at
    whatever rate the OS/device negotiated (commonly 44100/48000 Hz on
    Windows), so this is the one place that bridges the two: a polyphase
    filter (no pitch/speed distortion), skipped entirely when the rates
    already match.
    """
    if source_rate == target_rate or not pcm_bytes:
        return pcm_bytes

    from math import gcd

    import numpy as np
    from scipy.signal import resample_poly

    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    divisor = gcd(source_rate, target_rate)
    up = target_rate // divisor
    down = source_rate // divisor
    resampled = resample_poly(samples, up, down)
    return np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()


def pcm16_stats(pcm_bytes: bytes, sample_rate: int) -> dict[str, float | int]:
    """Diagnostic helper: basic signal stats for a raw PCM16 mono buffer,
    used to physically characterize what audio actually reached the backend
    (duration, clipping/peak, loudness, DC offset) without guessing."""
    import numpy as np

    if not pcm_bytes:
        return {
            "sample_count": 0,
            "duration_ms": 0.0,
            "peak_amplitude": 0.0,
            "rms": 0.0,
            "dc_offset": 0.0,
        }
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    return {
        "sample_count": samples.size,
        "duration_ms": round(samples.size / sample_rate * 1000.0, 1) if sample_rate else 0.0,
        "peak_amplitude": round(float(np.max(np.abs(samples))), 4),
        "rms": round(float(np.sqrt(np.mean(np.square(samples)))), 4),
        "dc_offset": round(float(np.mean(samples)), 5),
    }
