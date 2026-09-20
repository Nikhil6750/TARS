"""Scratch diagnostic (not production): captures a FIXED 5-second window
raw via PyAudio, bypassing SpeechRecognition's energy-based phrase
boundary detection entirely, to isolate whether the mic captures real
signal at all vs. whether phrase-boundary detection is mis-triggering."""
from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "apps" / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pyaudio  # noqa: E402

from voice.mic_device import resolve_default_input_device  # noqa: E402

DURATION_S = 5.0
OUT_PATH = REPO_ROOT / "artifacts" / "voice-selftest" / "fixed_capture.wav"


def main() -> int:
    mic = resolve_default_input_device()
    print(f"Recording {DURATION_S}s from: {mic.name} @ {mic.sample_rate} Hz")
    print("SAY NOW (recording starts immediately for a fixed 5 seconds):")
    print('  "Explain Docker containers"')

    audio = pyaudio.PyAudio()
    stream = audio.open(
        input_device_index=mic.device_index,
        channels=1,
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

    pcm = b"".join(frames)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(OUT_PATH), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(mic.sample_rate)
        w.writeframes(pcm)

    import numpy as np

    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    print(f"Saved: {OUT_PATH}")
    print(f"Samples: {samples.size}  duration_s={samples.size / mic.sample_rate:.2f}")
    print(f"Peak: {float(np.max(np.abs(samples))):.4f}")
    print(f"RMS: {float(np.sqrt(np.mean(samples**2))):.4f}")
    # Peak over each 0.5s window, to see whether ANY window has real signal.
    window = int(mic.sample_rate * 0.5)
    for i in range(0, samples.size - window, window):
        chunk = samples[i : i + window]
        print(
            f"  [{i / mic.sample_rate:.1f}s-{(i + window) / mic.sample_rate:.1f}s] "
            f"peak={float(np.max(np.abs(chunk))):.4f} rms={float(np.sqrt(np.mean(chunk**2))):.4f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
