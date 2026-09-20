"""ElevenLabs realtime streaming TTS -- the premium/default connected voice
when `ELEVENLABS_API_KEY`/`ELEVENLABS_VOICE_ID` are configured (see
`.env.example`). SAPI (`voice/providers/sapi_tts.py`) and Kokoro
(`voice/providers/kokoro_tts.py`) remain the fallback chain; this provider
never blocks startup and never raises for a missing key -- `is_available`
is False and callers (voice/gemini_live_loop.py's `_speak_chunk`, this
module's own `synthesize()`) treat that exactly like any other unconfigured
optional provider.

Protocol: the current official realtime WebSocket endpoint --
`wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input` -- with
`model_id=eleven_flash_v2_5` (lowest-latency model) and
`output_format=pcm_{sample_rate}` (raw 16-bit PCM straight from the API, no
MP3 decode and no resample needed when `sample_rate` is chosen to match the
playback device's rate, e.g. 24000 in gemini_live_loop.py). Deliberately
NOT using the deprecated `optimize_streaming_latency` query parameter --
`auto_mode=true` is used instead so ElevenLabs decides generation
boundaries itself rather than this client hand-tuning buffering.

One WebSocket connection is reused across every sentence of one TARS turn
(`stream_chunk` calls share `_ws` until `end_turn()` closes it) rather than
reconnecting per sentence -- reconnecting per tiny chunk is exactly what
Part G of the ElevenLabs integration spec this was built against forbids.
`synthesize()` (the plain `TextToSpeechProvider` interface method, used by
the non-live REST voice path and by benchmarks) opens a connection, sends
one block of text, collects all audio, and closes -- one-shot, not meant to
be reused.

Every failure raises `VoiceProviderError` and never leaves a half-open
connection behind (`_ws` is reset to None on any error), so callers can
retry or fall back to SAPI/Kokoro without special-casing this provider.

This module has not been exercised against the live ElevenLabs API in this
environment (no `ELEVENLABS_API_KEY` was configured when it was written) --
the wire protocol above is implemented to ElevenLabs' documented realtime
TTS WebSocket contract as of this writing, but the exact timing of
`isFinal` messages is asserted defensively (a read timeout is treated as
"this chunk is done, fall back if nothing was said at all") rather than
assumed, specifically so an undocumented protocol edge case fails safe into
the SAPI fallback instead of hanging a turn.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import AsyncIterator

from voice.audio_utils import pcm16_to_wav
from voice.errors import VoiceProviderError
from voice.interfaces import SynthesisResult, TextToSpeechProvider

logger = logging.getLogger("tars.voice.elevenlabs_tts")

DEFAULT_MODEL_ID = "eleven_flash_v2_5"
DEFAULT_SAMPLE_RATE = 24000
# Time budget to complete the WebSocket handshake. ElevenLabs Flash is a
# low-latency model; a connect this slow means the network path itself is
# the bottleneck, not worth waiting longer on a voice turn.
CONNECT_TIMEOUT_SECONDS = 4.0
# Time budget for the FIRST audio byte of one flushed text chunk. Generous
# enough for a real Flash-model round trip over the public internet, tight
# enough that a stalled/broken stream falls back to SAPI within one turn
# rather than leaving TARS silent.
FIRST_AUDIO_TIMEOUT_SECONDS = 6.0
# Time budget between subsequent messages once audio has started arriving
# for one chunk -- covers the tail of one sentence's generation.
CHUNK_IDLE_TIMEOUT_SECONDS = 8.0


class ElevenLabsTTSProvider(TextToSpeechProvider):
    name = "elevenlabs"

    def __init__(
        self,
        *,
        api_key: str | None,
        voice_id: str | None,
        model_id: str = DEFAULT_MODEL_ID,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> None:
        self._api_key = api_key
        self._voice_id = voice_id
        self._model_id = model_id
        self._sample_rate = sample_rate
        self._ws = None
        self._ws_lock = asyncio.Lock()
        # Set once per connection by _ensure_connected; read by callers that
        # want the CONNECT_MS figure for latency logging (see
        # gemini_live_loop.py's per-turn LATENCY_MS line).
        self.last_connect_ms: float | None = None

    @property
    def is_available(self) -> bool:
        return bool(self._api_key and self._voice_id)

    async def synthesize(self, text: str) -> SynthesisResult:
        """One-shot: whole text in, whole WAV out. Used by the generic REST
        voice path (voice/factory.py) and local benchmarking -- never
        reused across calls, always closes its own connection."""
        chunks = [pcm async for pcm in self._stream_text(text, close_after=True)]
        audio = b"".join(chunks)
        if not audio:
            raise VoiceProviderError("elevenlabs synthesis produced no audio")
        return SynthesisResult(
            audio=pcm16_to_wav(audio, self._sample_rate), sample_rate=self._sample_rate
        )

    async def stream_chunk(self, text: str) -> AsyncIterator[bytes]:
        """Streams raw PCM16 chunks (no WAV header) for one sentence over a
        connection kept open across calls within the same turn. Caller
        (gemini_live_loop.py's _speak_chunk) writes each chunk to the
        playback device as it arrives -- true incremental streaming, not
        buffer-then-play. Call `end_turn()` once the turn's speech is
        entirely done; do not call this again afterward without a fresh
        connection being opened automatically on the next call."""
        async for pcm in self._stream_text(text, close_after=False):
            yield pcm

    async def end_turn(self) -> None:
        """Closes the connection reused across one turn's sentences. Safe
        to call even if no connection is open (e.g. every chunk this turn
        failed over to SAPI already)."""
        async with self._ws_lock:
            await self._close_locked()

    async def _close_locked(self) -> None:
        if self._ws is None:
            return
        ws = self._ws
        self._ws = None
        try:
            await ws.send(json.dumps({"text": ""}))
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass

    async def _ensure_connected_locked(self) -> None:
        if self._ws is not None:
            return
        import websockets

        uri = (
            f"wss://api.elevenlabs.io/v1/text-to-speech/{self._voice_id}/stream-input"
            f"?model_id={self._model_id}&output_format=pcm_{self._sample_rate}"
            "&auto_mode=true"
        )
        started = time.monotonic()
        try:
            ws = await asyncio.wait_for(
                websockets.connect(uri, extra_headers={"xi-api-key": self._api_key}),
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            raise VoiceProviderError(f"elevenlabs connect failed: {exc}") from exc
        self.last_connect_ms = (time.monotonic() - started) * 1000
        try:
            await ws.send(
                json.dumps(
                    {
                        "text": " ",
                        "voice_settings": {
                            "stability": 0.5,
                            "similarity_boost": 0.8,
                            "use_speaker_boost": False,
                        },
                        # Redundant with the xi-api-key header above --
                        # kept because ElevenLabs' own examples send it in
                        # the BOS message too, and a proxy stripping custom
                        # headers is a cheaper failure mode to guard against
                        # here than to debug later.
                        "xi_api_key": self._api_key,
                    }
                )
            )
        except Exception as exc:
            try:
                await ws.close()
            except Exception:
                pass
            raise VoiceProviderError(f"elevenlabs handshake failed: {exc}") from exc
        self._ws = ws

    async def _stream_text(self, text: str, *, close_after: bool) -> AsyncIterator[bytes]:
        if not self.is_available:
            raise VoiceProviderError(
                "elevenlabs: no ELEVENLABS_API_KEY/ELEVENLABS_VOICE_ID configured"
            )
        if not text.strip():
            return
        async with self._ws_lock:
            await self._ensure_connected_locked()
            ws = self._ws
            assert ws is not None
            try:
                await ws.send(json.dumps({"text": text + " ", "flush": True}))
            except Exception as exc:
                await self._close_locked()
                raise VoiceProviderError(f"elevenlabs send failed: {exc}") from exc

            got_audio = False
            timeout = FIRST_AUDIO_TIMEOUT_SECONDS
            try:
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    except TimeoutError:
                        # No more audio arrived within budget. Treated as
                        # "this chunk is done" rather than an error if we
                        # already got audio -- ElevenLabs' isFinal timing
                        # per flushed chunk is not something this client
                        # has verified against the live API (see module
                        # docstring), so a quiet stream after real audio is
                        # the expected end-of-chunk signal, not a failure.
                        if got_audio:
                            return
                        raise VoiceProviderError(
                            "elevenlabs: no audio received before timeout"
                        )
                    message = json.loads(raw)
                    audio_b64 = message.get("audio")
                    if audio_b64:
                        got_audio = True
                        timeout = CHUNK_IDLE_TIMEOUT_SECONDS
                        yield base64.b64decode(audio_b64)
                    if message.get("isFinal"):
                        return
            except VoiceProviderError:
                await self._close_locked()
                raise
            except Exception as exc:
                await self._close_locked()
                raise VoiceProviderError(f"elevenlabs stream failed: {exc}") from exc
            finally:
                if close_after:
                    await self._close_locked()
