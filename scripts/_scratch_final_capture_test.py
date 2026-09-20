"""Final verification: the proven Float32 WASAPI capture path, converted
correctly through to faster-whisper. Fixed 10-second window, printed
BEFORE recording starts so there's time to read and respond."""
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

from app.config import get_settings  # noqa: E402
from voice.audio_utils import pcm16_to_wav, resample_pcm16  # noqa: E402
from voice.mic_device import resolve_default_input_device  # noqa: E402
from voice.providers.faster_whisper_stt import FasterWhisperSTTProvider  # noqa: E402

DURATION_S = 10.0
OUT_DIR = REPO_ROOT / "artifacts" / "voice-selftest"
TARGET_RATE = 16000


def main() -> int:
    mic = resolve_default_input_device()
    settings = get_settings()

    provider = FasterWhisperSTTProvider(
        model_size=settings.faster_whisper_model,
        device=settings.faster_whisper_device,
        compute_type=settings.faster_whisper_compute_type,
    )

    print(f"Device: {mic.name} (index {mic.device_index})")
    print(f"Capture: paFloat32, {mic.sample_rate} Hz, 2 channels -> mono -> {TARGET_RATE} Hz")
    print()
    print("SAY NOW:")
    print('  "Explain Docker containers"')
    print(f"(You have {DURATION_S:.0f} seconds, starting now)")

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
    stereo = np.frombuffer(raw, dtype="<f4").reshape(-1, 2)
    mono_f32 = (stereo[:, 0] + stereo[:, 1]) / 2.0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = OUT_DIR / "final_test_native_48k_mono.wav"
    with wave.open(str(raw_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(mic.sample_rate)
        native_pcm16 = (np.clip(mono_f32, -1.0, 1.0) * 32767.0).astype("<i2")
        w.writeframes(native_pcm16.tobytes())

    resampled_pcm16 = resample_pcm16(native_pcm16.tobytes(), mic.sample_rate, TARGET_RATE)
    final_16k_path = OUT_DIR / "final_test_16k.wav"
    final_16k_path.write_bytes(pcm16_to_wav(resampled_pcm16, TARGET_RATE))

    samples_16k = np.frombuffer(resampled_pcm16, dtype="<i2").astype(np.float32) / 32768.0
    peak = float(np.max(np.abs(samples_16k))) if samples_16k.size else 0.0
    rms = float(np.sqrt(np.mean(samples_16k**2))) if samples_16k.size else 0.0

    t0 = time.monotonic()
    result = provider._transcribe_sync(resampled_pcm16)
    latency_s = time.monotonic() - t0

    print()
    print(f"CAPTURED DURATION: {mono_f32.size / mic.sample_rate:.2f} s (native {mic.sample_rate} Hz)")
    print(f"16K SAMPLE COUNT: {samples_16k.size}")
    print(f"PEAK: {peak:.4f}")
    print(f"RMS: {rms:.4f}")
    print(f"MODEL: {settings.faster_whisper_model}")
    print(f"TRANSCRIPT: {result.text!r}")
    print(f"STT LATENCY: {latency_s:.2f} s")
    print(f"SAVED NATIVE: {raw_path}")
    print(f"SAVED 16K: {final_16k_path}")

    normalized = result.text.lower()
    passed = "explain" in normalized and "docker" in normalized
    print(f"PASS/FAIL: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
