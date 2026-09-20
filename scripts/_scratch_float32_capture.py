"""Scratch diagnostic: same device/channels/rate as _scratch_native_capture.py
but requests paFloat32 instead of paInt16, to test whether PyAudio's Int16
request is hitting a lossy WASAPI format-conversion path on this device
(CPAL negotiated F32 for the same device and got real signal)."""
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


def main() -> int:
    mic = resolve_default_input_device()
    print(f"Device: {mic.name} (index {mic.device_index})")
    print(f"Format: paFloat32, {mic.sample_rate} Hz, 2 channels")
    print()
    print("SAY NOW (recording starts immediately for a fixed 5 seconds):")
    print('  "Explain Docker containers"')

    audio = pyaudio.PyAudio()
    stream = audio.open(
        input_device_index=mic.device_index,
        channels=2,
        format=pyaudio.paFloat32,
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
    interleaved = np.frombuffer(raw, dtype="<f4")
    stereo = interleaved.reshape(-1, 2)
    left = stereo[:, 0]
    right = stereo[:, 1]
    mono = (left + right) / 2.0

    def peak_rms(x: np.ndarray) -> tuple[float, float]:
        if x.size == 0:
            return 0.0, 0.0
        return float(np.max(np.abs(x))), float(np.sqrt(np.mean(x**2)))

    def to_int16(x: np.ndarray) -> np.ndarray:
        return np.clip(x, -1.0, 1.0).astype(np.float32) * 32767.0

    def write_wav(path: Path, x: np.ndarray, channels: int) -> None:
        with wave.open(str(path), "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(2)
            w.setframerate(mic.sample_rate)
            w.writeframes(to_int16(x).astype("<i2").tobytes())

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    interleaved_i16 = np.empty(stereo.size, dtype=np.float32)
    interleaved_i16[0::2] = left
    interleaved_i16[1::2] = right
    write_wav(OUT_DIR / "native_f32_stereo.wav", interleaved_i16, 2)
    write_wav(OUT_DIR / "native_f32_mono.wav", mono, 1)

    lp, lr = peak_rms(left)
    rp, rr = peak_rms(right)
    mp, mr = peak_rms(mono)
    print()
    print(f"Captured {stereo.shape[0] / mic.sample_rate:.2f}s")
    print(f"LEFT:  peak={lp:.4f}  rms={lr:.4f}")
    print(f"RIGHT: peak={rp:.4f}  rms={rr:.4f}")
    print(f"MONO:  peak={mp:.4f}  rms={mr:.4f}")

    window = int(mic.sample_rate * 0.5)
    print("\nPer-window breakdown:")
    for i in range(0, stereo.shape[0] - window, window):
        wlp, wlr = peak_rms(left[i : i + window])
        wrp, wrr = peak_rms(right[i : i + window])
        t0, t1 = i / mic.sample_rate, (i + window) / mic.sample_rate
        print(f"  [{t0:.1f}s-{t1:.1f}s] L peak={wlp:.4f} rms={wlr:.4f} | R peak={wrp:.4f} rms={wrr:.4f}")

    print(f"\nSaved: {OUT_DIR / 'native_f32_stereo.wav'}")
    print(f"Saved: {OUT_DIR / 'native_f32_mono.wav'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
