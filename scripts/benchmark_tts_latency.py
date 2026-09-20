"""Physically measures first-audio latency for each configured TTS provider
on THIS machine -- SAPI, Kokoro, and ElevenLabs if a key is configured.
Never fabricates a number: a provider that can't run (missing package,
missing key) is reported as SKIPPED with the reason, not a guessed value.

Run from the repo root:
    apps/backend/.venv/Scripts/python.exe scripts/benchmark_tts_latency.py
or with whatever interpreter has apps/backend/requirements*.txt installed.
"""
from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "backend"))

TEXT = "Docker containers package an application and its dependencies into an isolated, portable environment."
RUNS = 3


async def _bench_sapi() -> None:
    try:
        from voice.providers.sapi_tts import SapiTTSProvider
    except Exception as exc:
        print(f"SAPI: SKIPPED ({exc})")
        return
    try:
        provider = SapiTTSProvider()
    except Exception as exc:
        print(f"SAPI: SKIPPED ({exc})")
        return
    latencies = []
    for _ in range(RUNS):
        started = time.monotonic()
        result = await provider.synthesize(TEXT)
        latencies.append((time.monotonic() - started) * 1000)
        assert result.audio
    _report("SAPI", latencies)


async def _bench_kokoro() -> None:
    try:
        from voice.providers.kokoro_tts import KokoroTTSProvider
    except Exception as exc:
        print(f"Kokoro: SKIPPED ({exc})")
        return
    try:
        provider = KokoroTTSProvider()
    except Exception as exc:
        print(f"Kokoro: SKIPPED ({exc})")
        return
    latencies = []
    for _ in range(RUNS):
        started = time.monotonic()
        result = await provider.synthesize(TEXT)
        latencies.append((time.monotonic() - started) * 1000)
        assert result.audio
    _report("Kokoro", latencies)


async def _bench_elevenlabs() -> None:
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID")
    if not api_key or not voice_id:
        print("ElevenLabs: SKIPPED (no ELEVENLABS_API_KEY/ELEVENLABS_VOICE_ID in environment)")
        return
    from voice.providers.elevenlabs_tts import ElevenLabsTTSProvider

    latencies = []
    first_audio_latencies = []
    for _ in range(RUNS):
        provider = ElevenLabsTTSProvider(api_key=api_key, voice_id=voice_id)
        started = time.monotonic()
        first_audio_at = None
        async for _pcm in provider.stream_chunk(TEXT):
            if first_audio_at is None:
                first_audio_at = time.monotonic()
        await provider.end_turn()
        if first_audio_at is not None:
            first_audio_latencies.append((first_audio_at - started) * 1000)
        latencies.append((time.monotonic() - started) * 1000)
    if first_audio_latencies:
        _report("ElevenLabs (first audio byte)", first_audio_latencies)
    _report("ElevenLabs (full chunk complete)", latencies)


def _report(name: str, latencies_ms: list[float]) -> None:
    if not latencies_ms:
        print(f"{name}: SKIPPED (no successful runs)")
        return
    mean = statistics.mean(latencies_ms)
    print(
        f"{name}: {mean:.1f}ms mean over {len(latencies_ms)} run(s) "
        f"(min={min(latencies_ms):.1f}ms max={max(latencies_ms):.1f}ms) "
        f"runs={[round(v, 1) for v in latencies_ms]}"
    )


async def main() -> None:
    print(f"Benchmarking first-audio latency, {RUNS} run(s) each, text={TEXT!r}\n")
    await _bench_sapi()
    await _bench_kokoro()
    await _bench_elevenlabs()


if __name__ == "__main__":
    asyncio.run(main())
