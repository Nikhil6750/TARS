"""Scratch diagnostic (not production): captures the WASAPI default
microphone at its NATIVE format (48000 Hz, 2 channels, PCM16) with NO
resampling, downmixing, VAD, noise gating, or STT -- just raw capture and
per-channel isolation, to find whether the problem is upstream of STT
entirely (device/channel/format) before touching Whisper again."""
from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "apps" / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import numpy as np  # noqa: E402
import pyaudio  # noqa: E402

from voice.mic_device import resolve_default_input_device  # noqa: E402

DURATION_S = 5.0
OUT_DIR = REPO_ROOT / "artifacts" / "voice-selftest"


def _write_wav(path: Path, samples_int16: np.ndarray, channels: int, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(samples_int16.astype("<i2").tobytes())


def _peak_rms(samples_int16: np.ndarray) -> tuple[float, float]:
    if samples_int16.size == 0:
        return 0.0, 0.0
    floats = samples_int16.astype(np.float32) / 32768.0
    return float(np.max(np.abs(floats))), float(np.sqrt(np.mean(floats**2)))


def main() -> int:
    mic = resolve_default_input_device()
    print(f"Device: {mic.name} (index {mic.device_index})")
    print(f"Native format: {mic.sample_rate} Hz, 2 channels, PCM16 -- raw, no processing")
    print()
    print("SAY NOW (recording starts immediately for a fixed 5 seconds):")
    print('  "Explain Docker containers"')

    audio = pyaudio.PyAudio()
    stream = audio.open(
        input_device_index=mic.device_index,
        channels=2,
        format=pyaudio.paInt16,
        rate=mic.sample_rate,
        frames_per_buffer=1024,
        input=True,
    )
    frames = []
    start = time.monotonic()
    while time.monotonic() - start < DURATION_S:
        frames.append(stream.read(1024, exception_on_overflow=False))
    stream.stop_stream()
    stream.close()
    audio.terminate()

    raw = b"".join(frames)
    interleaved = np.frombuffer(raw, dtype="<i2")
    stereo = interleaved.reshape(-1, 2)  # columns: [left, right]
    left = stereo[:, 0]
    right = stereo[:, 1]
    mono = ((left.astype(np.int32) + right.astype(np.int32)) // 2).astype(np.int16)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    _write_wav(OUT_DIR / "native_stereo.wav", stereo.reshape(-1), 2, mic.sample_rate)
    _write_wav(OUT_DIR / "native_left.wav", left, 1, mic.sample_rate)
    _write_wav(OUT_DIR / "native_right.wav", right, 1, mic.sample_rate)
    _write_wav(OUT_DIR / "native_mono.wav", mono, 1, mic.sample_rate)

    duration_s = stereo.shape[0] / mic.sample_rate
    left_peak, left_rms = _peak_rms(left)
    right_peak, right_rms = _peak_rms(right)
    mono_peak, mono_rms = _peak_rms(mono)

    print()
    print(f"Captured {duration_s:.2f}s, {stereo.shape[0]} frames/channel")
    print(f"LEFT:  peak={left_peak:.4f}  rms={left_rms:.4f}")
    print(f"RIGHT: peak={right_peak:.4f}  rms={right_rms:.4f}")
    print(f"MONO:  peak={mono_peak:.4f}  rms={mono_rms:.4f}")
    print()

    # Per-0.5s window breakdown for each channel, to localize when/where
    # any real signal actually occurred.
    window = int(mic.sample_rate * 0.5)
    print("Per-window breakdown (peak/rms):")
    for i in range(0, stereo.shape[0] - window, window):
        lp, lr = _peak_rms(left[i : i + window])
        rp, rr = _peak_rms(right[i : i + window])
        t0, t1 = i / mic.sample_rate, (i + window) / mic.sample_rate
        print(f"  [{t0:.1f}s-{t1:.1f}s] L peak={lp:.4f} rms={lr:.4f} | R peak={rp:.4f} rms={rr:.4f}")

    print()
    print("Saved (raw, unprocessed -- no Whisper run yet):")
    print(f"  {OUT_DIR / 'native_stereo.wav'}")
    print(f"  {OUT_DIR / 'native_left.wav'}")
    print(f"  {OUT_DIR / 'native_right.wav'}")
    print(f"  {OUT_DIR / 'native_mono.wav'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
