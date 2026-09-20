"""Windows SAPI voice -- the default hot-path FAST_TTS for the Gemini
voice loop (see voice/gemini_live_loop.py). Physically measured on this
machine: ~30-90ms to synthesize a full sentence to a WAV file, because
SAPI's built-in voices are a real-time-capable, non-neural synthesis
engine -- when writing to a file stream (not paced to a live audio device)
it runs many times faster than real-time. Kokoro (voice/providers/
kokoro_tts.py), by contrast, is a CPU-bound ONNX neural model measured at
roughly 1-4 seconds per sentence on this machine -- fine for offline
quality, far too slow for a voice assistant that should feel immediate.
Kokoro remains available as the OFFLINE_QUALITY_FALLBACK gemini_live_loop.py
falls back to if SAPI synthesis fails for any reason.

Ships no bundled voice; uses whatever SAPI voices Windows already has
installed (there are always at least the built-in ones) -- no model
download, per the "do not download large models" requirement this was
built to satisfy.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
import wave

from voice.errors import VoiceProviderError
from voice.interfaces import SynthesisResult, TextToSpeechProvider

# SpeechStreamFileMode.SSFMCreateForWrite -- stable, documented SAPI5 value.
_SSFM_CREATE_FOR_WRITE = 3


class SapiTTSProvider(TextToSpeechProvider):
    name = "sapi"

    def __init__(self, voice_name_contains: str | None = None) -> None:
        try:
            import win32com.client  # noqa: F401
        except ImportError as exc:
            raise VoiceProviderError(
                "pywin32 (win32com) is required for SAPI TTS -- Windows only"
            ) from exc
        self._voice_name_contains = voice_name_contains

    async def synthesize(self, text: str) -> SynthesisResult:
        return await asyncio.to_thread(self._synthesize_sync, text)

    def _synthesize_sync(self, text: str) -> SynthesisResult:
        import pythoncom
        import win32com.client

        # asyncio.to_thread() runs this on a worker thread from the
        # default executor's pool, which COM has never initialized --
        # every COM call fails with "CoInitialize has not been called"
        # otherwise (physically reproduced). CoInitialize is idempotent
        # per-thread (returns S_FALSE, not an error, if already called),
        # and thread-pool threads are reused across calls, so this is safe
        # to call every time without a matching CoUninitialize.
        pythoncom.CoInitialize()

        path = os.path.join(tempfile.gettempdir(), f"tars-sapi-{uuid.uuid4().hex}.wav")
        try:
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            if self._voice_name_contains:
                voices = voice.GetVoices()
                for i in range(voices.Count):
                    candidate = voices.Item(i)
                    if self._voice_name_contains.lower() in candidate.GetDescription().lower():
                        voice.Voice = candidate
                        break
            stream = win32com.client.Dispatch("SAPI.SpFileStream")
            stream.Open(path, _SSFM_CREATE_FOR_WRITE, False)
            voice.AudioOutputStream = stream
            try:
                voice.Speak(text, 0)  # synchronous; writes directly to the file stream
            finally:
                stream.Close()
            # Read back the WAV SAPI actually wrote rather than assume a
            # sample rate -- physically verified that the format SAPI
            # writes doesn't match what its own `SpAudioFormat.Type`
            # constants nominally request, so trust the file header only.
            with wave.open(path, "rb") as wav_file:
                sample_rate = wav_file.getframerate()
            with open(path, "rb") as f:
                wav_bytes = f.read()
        except VoiceProviderError:
            raise
        except Exception as exc:
            raise VoiceProviderError(f"SAPI synthesis failed: {exc}") from exc
        finally:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        if not wav_bytes:
            raise VoiceProviderError("SAPI synthesis produced no audio")
        return SynthesisResult(audio=wav_bytes, sample_rate=sample_rate)
