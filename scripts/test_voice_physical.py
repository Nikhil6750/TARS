"""Physical microphone + STT self-test.

Uses the EXACT SAME microphone resolution (voice.mic_device), capture
library (SpeechRecognition/PyAudio), and STT provider
(voice.providers.faster_whisper_stt.FasterWhisperSTTProvider) as the
production backend voice loop. No fixtures, no injected audio, no faked
transcript -- a real microphone must be present and physically used.

Run:

    python scripts/test_voice_physical.py
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "apps" / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import speech_recognition as sr
from app.config import get_settings
from voice.audio_utils import wav_to_pcm16
from voice.mic_device import resolve_default_input_device
from voice.providers.faster_whisper_stt import FasterWhisperSTTProvider

EXPECTED_PHRASE = "explain docker containers"
OUTPUT_DIR = REPO_ROOT / "artifacts" / "voice-selftest"


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9\s]", "", text.lower()).strip()


def _wav_stats(pcm_bytes: bytes, sample_rate: int) -> dict[str, float]:
    import numpy as np

    if not pcm_bytes:
        return {"duration_ms": 0.0, "peak": 0.0, "rms": 0.0}
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    return {
        "duration_ms": round(samples.size / sample_rate * 1000.0, 1),
        "peak": round(float(np.max(np.abs(samples))), 4),
        "rms": round(float(np.sqrt(np.mean(samples**2))), 4),
    }


def main() -> int:
    print("=== TARS physical voice self-test ===\n")

    print("Available input devices:")
    for name in sr.Microphone.list_microphone_names():
        print(f"  {name}")
    print()

    mic_info = resolve_default_input_device()
    print(
        f"Selected microphone: {mic_info.name} "
        f"(index {mic_info.device_index}, native {mic_info.sample_rate} Hz)"
    )

    settings = get_settings()
    print(
        f"Loading faster-whisper model={settings.faster_whisper_model} "
        f"device={settings.faster_whisper_device} "
        f"compute_type={settings.faster_whisper_compute_type} ..."
    )
    provider = FasterWhisperSTTProvider(
        model_size=settings.faster_whisper_model,
        device=settings.faster_whisper_device,
        compute_type=settings.faster_whisper_compute_type,
    )
    print("Model loaded.\n")

    recognizer = sr.Recognizer()
    microphone = sr.Microphone(
        device_index=mic_info.device_index, sample_rate=mic_info.sample_rate
    )
    with microphone as source:
        print("Calibrating ambient noise (stay quiet for a moment)...")
        recognizer.adjust_for_ambient_noise(source, duration=1.0)
        print(f"Ambient energy threshold: {recognizer.energy_threshold:.1f}\n")
        print("SAY NOW (you have 45 seconds):")
        print('  "Explain Docker containers"\n')
        try:
            audio = recognizer.listen(source, timeout=45, phrase_time_limit=8)
        except sr.WaitTimeoutError:
            print("PASS/FAIL: FAIL (no speech detected within 45 seconds)")
            return 1

    wav_bytes = audio.get_wav_data(convert_rate=16000, convert_width=2)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / "latest.wav"
    out_path.write_bytes(wav_bytes)

    pcm, sample_rate = wav_to_pcm16(wav_bytes)
    stats = _wav_stats(pcm, sample_rate)

    t0 = time.monotonic()
    result = provider._transcribe_sync(pcm)
    latency_s = time.monotonic() - t0

    normalized = _normalize(result.text)
    passed = EXPECTED_PHRASE in normalized

    print(f"MIC: {mic_info.name}")
    print(f"CAPTURE DURATION: {stats['duration_ms']} ms")
    print(f"RMS: {stats['rms']}")
    print(f"PEAK: {stats['peak']}")
    print(
        f"MODEL: {settings.faster_whisper_model} "
        f"(device={settings.faster_whisper_device}, "
        f"compute_type={settings.faster_whisper_compute_type})"
    )
    print(f"TRANSCRIPT: {result.text!r}")
    print(f"STT LATENCY: {latency_s:.2f} s")
    print(f"SAVED WAV: {out_path}")
    print(f"PASS/FAIL: {'PASS' if passed else 'FAIL'}")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
