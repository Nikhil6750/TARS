"""Optional CPU streaming Pocket TTS, with bounded transport and cancellation.

Model access is serialized; cancellation discards the current clause's residual
decoder work, and never starts another clause. Kokoro remains the fallback.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading

from voice.audio_utils import float32_to_pcm16, pcm16_to_wav
from voice.interfaces import SynthesisResult, TextToSpeechProvider


class PocketTTSProvider(TextToSpeechProvider):
    name = "pocket"

    def __init__(self, voice: str = "alba"):
        import torch
        from pocket_tts import TTSModel

        torch.set_num_threads(2)
        self._model = TTSModel.load_model()
        self._state = self._model.get_state_for_audio_prompt(voice)
        self._lock = threading.Lock()
        # First synthesis in a process pays a one-off warm-up (measured ~10 s). Pay it at startup,
        # not on the user's first spoken answer.
        try:
            for _ in self._model.generate_audio_stream(self._state, "Ready."):
                pass
        except Exception:
            pass

    async def synthesize_stream(self, text: str):
        queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        stopped = threading.Event()
        loop = asyncio.get_running_loop()

        def put(item):
            if stopped.is_set():
                return
            future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
            while not stopped.is_set():
                try:
                    future.result(timeout=0.05)
                    return
                except concurrent.futures.TimeoutError:
                    continue
            future.cancel()

        def produce():
            try:
                with self._lock:
                    if stopped.is_set():
                        return
                    # Complete/drain the in-flight decoder before another call uses
                    # this non-thread-safe model. Cancelled chunks are never sent.
                    for samples in self._model.generate_audio_stream(self._state, text):
                        if not stopped.is_set():
                            rate = self._model.sample_rate
                            audio = pcm16_to_wav(float32_to_pcm16(samples.detach().cpu().numpy()), rate)
                            put(SynthesisResult(audio, rate))
            except Exception as exc:
                put(exc)
            finally:
                put(None)

        worker = asyncio.create_task(asyncio.to_thread(produce))
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
            await worker
        finally:
            stopped.set()
            # Worker owns model cleanup; observe errors without delaying barge-in.
            worker.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

    async def synthesize(self, text: str) -> SynthesisResult:

        pcm = bytearray()
        rate = self._model.sample_rate
        async for chunk in self.synthesize_stream(text):
            # wav_to_pcm16 resamples to 16kHz, so decode WAV directly here.
            import io
            import wave
            with wave.open(io.BytesIO(chunk.audio), "rb") as wav:
                pcm.extend(wav.readframes(wav.getnframes()))
        return SynthesisResult(pcm16_to_wav(bytes(pcm), rate), rate)
