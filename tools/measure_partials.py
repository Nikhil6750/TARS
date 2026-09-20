"""Measure partial-transcript cadence of IncrementalWhisperSTT on synthetic speech.

Synthetic (Pocket TTS) speech fed at real-time pace through the real Silero VAD
and the real faster-whisper model. This measures decode/endpoint behaviour only;
it is NOT a microphone test.

python tools/measure_partials.py --model base.en [--text "..."] [--interval 0.48]
"""
import argparse
import asyncio
import io
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/backend"))

from voice.providers.faster_whisper_stt import FasterWhisperSTTProvider  # noqa: E402
import os  # noqa: E402
from voice.streaming import IncrementalWhisperSTT, SherpaPartialEngine, SileroStreamingVAD  # noqa: E402


def speech_16k(text: str) -> bytes:
    import torch
    from pocket_tts import TTSModel

    torch.set_num_threads(2)
    model = TTSModel.load_model()
    state = model.get_state_for_audio_prompt("alba")
    parts = [c.detach().cpu().numpy().reshape(-1) for c in model.generate_audio_stream(state, text)]
    audio = np.concatenate(parts)
    rate = model.sample_rate
    n = int(len(audio) * 16000 / rate)
    audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio)
    return (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="base.en")
    ap.add_argument("--text", default="TARS, what is happening with EURUSD?")
    ap.add_argument("--interval", type=float, default=0.48)
    ap.add_argument("--wav", default="")
    ap.add_argument("--sherpa", action="store_true")
    a = ap.parse_args()
    if a.wav:
        with wave.open(a.wav, "rb") as w:
            pcm = w.readframes(w.getnframes())
    else:
        pcm = speech_16k(a.text)
    silence = bytes(2 * 16000)
    stream = silence[:16000] + pcm + silence  # 0.5 s lead, 1 s trail
    provider = FasterWhisperSTTProvider(a.model, "cpu", "int8")
    decode_ms: list[float] = []
    orig = provider._transcribe_sync

    def timed(p):
        t = time.perf_counter()
        r = orig(p)
        decode_ms.append((time.perf_counter() - t) * 1000)
        return r

    provider._transcribe_sync = timed
    await provider.transcribe(silence[:16000])  # warm-up (model/threads), as the server does at start
    decode_ms.clear()
    t0 = time.perf_counter()
    log = []

    async def emit(ev):
        log.append((round((time.perf_counter() - t0) * 1000), ev["type"], ev.get("text", "")))

    eng = SherpaPartialEngine(os.path.expanduser('~/.cache/tars-models/sherpa-onnx-streaming-zipformer-en-20M-2023-02-17')) if a.sherpa else None
    stt = IncrementalWhisperSTT(provider, emit, SileroStreamingVAD(), partial_interval=a.interval, partial_engine=eng)
    await stt.start()
    frame = 1024  # 32 ms of PCM16 mono
    for i in range(0, len(stream), frame * 2):
        due = t0 + (i / 2) / 16000
        await asyncio.sleep(max(0, due - time.perf_counter()))
        await stt.push_audio(stream[i:i + frame * 2])
    await asyncio.sleep(3)
    await stt.stop()
    speech_end_ms = 500 + len(pcm) / 2 / 16
    print(f"model={a.model} interval={a.interval} speech_ends_at~{speech_end_ms:.0f}ms")
    seen = set()
    for row in log:
        if row[1] in ('endpointing', 'speech_resumed') and row[1] in seen:
            continue
        seen.add(row[1]) if row[1] in ('endpointing','speech_resumed') else None
        print(row)
    print("decode_ms:", [round(x) for x in decode_ms])


asyncio.run(main())
