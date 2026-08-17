"""Local Fish Speech TTS — the preferred production TTS candidate per
ADR-011. Fish Speech is not a pip-installable inference library; it runs as
a separate local API server the user starts from the fish-speech repo
(`python tools/api_server.py --llama-checkpoint-path ... --listen
0.0.0.0:8080`, see https://speech.fish.audio/server/). This adapter is an
HTTP client against that already-running local server — it never launches
or manages the server process itself, and never talks to Fish Audio's
hosted cloud API (that would be the separate, optional `fish_audio_hosted`
provider named in ARCHITECTURE.md, not implemented here since it requires
a paid key and isn't part of the required local path).

The server's `/v1/tts` request/response body isn't fully published in
Fish Speech's docs at the time this was written (only the endpoint path and
a `GET /v1/health` check were confirmed) — this client sends the minimal
`{"text": ...}` JSON body and treats the response as raw audio bytes,
which is the documented Python client's usage pattern. Verify against the
server version actually deployed before relying on this in production; see
the handoff for what could and could not be confirmed in this environment.
"""
from __future__ import annotations

import httpx

from voice.audio_utils import pcm16_to_wav
from voice.errors import VoiceProviderError
from voice.interfaces import SynthesisResult, TextToSpeechProvider

DEFAULT_SAMPLE_RATE = 44100


class FishSpeechTTSProvider(TextToSpeechProvider):
    name = "fish_speech"

    def __init__(self, api_url: str, reference_id: str | None = None, timeout_seconds: float = 60.0):
        if not api_url:
            raise VoiceProviderError(
                "FishSpeechTTSProvider selected but FISH_SPEECH_API_URL is not "
                "set — start a local Fish Speech API server (see "
                "https://speech.fish.audio/server/) and point this at it, or "
                "use TTS_PROVIDER=kokoro instead"
            )
        self._api_url = api_url.rstrip("/")
        self._reference_id = reference_id
        self._timeout = timeout_seconds

    async def synthesize(self, text: str) -> SynthesisResult:
        payload: dict = {"text": text, "format": "pcm"}
        if self._reference_id:
            payload["reference_id"] = self._reference_id

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(f"{self._api_url}/v1/tts", json=payload)
        except httpx.ConnectError as exc:
            raise VoiceProviderError(
                f"Could not reach Fish Speech API server at {self._api_url} — "
                "is `tools/api_server.py` running?"
            ) from exc
        except httpx.TimeoutException as exc:
            raise VoiceProviderError("Fish Speech API server request timed out") from exc

        if resp.status_code != 200:
            raise VoiceProviderError(
                f"Fish Speech API server returned HTTP {resp.status_code}: {resp.text[:300]}"
            )

        audio_bytes = resp.content
        if not audio_bytes:
            raise VoiceProviderError("Fish Speech API server returned no audio data")

        if audio_bytes[:4] == b"RIFF":
            return SynthesisResult(audio=audio_bytes, sample_rate=DEFAULT_SAMPLE_RATE)
        return SynthesisResult(
            audio=pcm16_to_wav(audio_bytes, DEFAULT_SAMPLE_RATE), sample_rate=DEFAULT_SAMPLE_RATE
        )
