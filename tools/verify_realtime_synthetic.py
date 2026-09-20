"""Real local ASR/VAD/TTS inference with synthetic input, NOT a physical PASS."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/backend"))


async def run():
    from app.config import Settings
    from voice.audio_utils import wav_to_pcm16
    from voice.factory import build_stt_provider
    from voice.providers.pocket_tts import PocketTTSProvider
    from voice.streaming import IncrementalWhisperSTT, SileroStreamingVAD

    settings = Settings()
    tts = await asyncio.to_thread(PocketTTSProvider)
    stt = await asyncio.to_thread(build_stt_provider, settings)
    if stt.name == "mock":
        raise RuntimeError("Configure STT_PROVIDER=faster_whisper before real verification")
    first_audio = []
    results = []
    for text in ["TARS, what is happening with EURUSD?", "Stop. What about gold?", "TARS, what is happening with EURUSD?"]:
        started = time.perf_counter()
        audio = bytearray()
        import io
        import wave
        from voice.audio_utils import pcm16_to_wav
        rate = 24000
        async for chunk in tts.synthesize_stream(text):
            if not audio:
                first_audio.append(round((time.perf_counter() - started) * 1000, 2))
            with wave.open(io.BytesIO(chunk.audio), "rb") as wav:
                audio.extend(wav.readframes(wav.getnframes()))
                rate = wav.getframerate()
        pcm, _ = wav_to_pcm16(pcm16_to_wav(bytes(audio), rate))
        import numpy as np
        from scipy.signal import resample_poly
        from math import gcd
        divisor = gcd(rate, 16000)
        samples = resample_poly(np.frombuffer(pcm, dtype="<i2").astype(np.float32),
                                16000 // divisor, rate // divisor)
        pcm = np.clip(samples, -32768, 32767).astype("<i2").tobytes()
        events = []
        final = asyncio.Event()
        async def emit(event):
            events.append({**event, "observed_at": time.perf_counter()})
            if event["type"] in {"partial_transcript", "final_transcript", "provider_error"}:
                print(json.dumps(event), flush=True)
            if event["type"] == "final_transcript":
                final.set()
        provider = IncrementalWhisperSTT(stt, emit, SileroStreamingVAD())
        await provider.start()
        try:
            pcm += bytes(32000)
            for index in range(0, len(pcm), 1024):
                await provider.push_audio(pcm[index:index + 1024])
                await asyncio.sleep(0.032)
            await asyncio.wait_for(final.wait(), 20)
            ended = next(e for e in events if e["type"] == "speech_ended")
            finalized = next(e for e in events if e["type"] == "final_transcript")
            partials = [e for e in events if e["type"] == "partial_transcript"]
            results.append({"input": text, "final": finalized["text"],
                "partials": len(partials),
                "partial_during_speech": any(e["observed_at"] < ended["at"] for e in partials),
                "speech_ended_to_final_ms": round((finalized["observed_at"] - ended["at"]) * 1000, 2)})
        finally:
            await provider.stop()
    import statistics
    print(json.dumps({"physical_test": False, "stt": stt.name,
        "stt_model": settings.faster_whisper_model, "results": results,
        "tts_first_audio_ms": first_audio,
        "tts_first_audio_p50_ms": statistics.median(first_audio),
        "speech_ended_to_final_p50_ms": statistics.median(r["speech_ended_to_final_ms"] for r in results)}, indent=2))
    if not all(r["partial_during_speech"] and r["final"] for r in results):
        raise SystemExit("Synthetic streaming verification incomplete")


if __name__ == "__main__":
    asyncio.run(run())
