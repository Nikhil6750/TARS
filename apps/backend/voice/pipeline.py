"""Constructs the realtime Pipecat voice pipeline: WebSocket transport ->
Silero VAD -> STT -> TARS assistant bridge -> TTS -> WebSocket transport,
per ARCHITECTURE.md § Voice orchestration. Every stage is provider-neutral —
STT/TTS come from whichever `SpeechToTextProvider`/`TextToSpeechProvider`
`voice/factory.py` built from Settings, wrapped in the generic
`ProviderBridge*Service` classes (voice/pipecat_services.py) rather than a
vendor-specific Pipecat service, so the realtime session and the one-shot
REST transcribe/synthesize endpoints (Phase E) share one provider instance
and one code path per adapter.

Uses Pipecat's current (1.7.0+) worker-based execution model —
`PipelineWorker` + `WorkerRunner` — not the deprecated `PipelineTask` /
`PipelineRunner` aliases still shown in some older Pipecat examples.
"""
from __future__ import annotations

import logging

from fastapi import WebSocket
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.workers.runner import WorkerRunner

from actions.runtime import ActionRuntime
from assistant.router import AssistantRouter
from voice.interfaces import SpeechToTextProvider, TextToSpeechProvider
from voice.pipecat_bridge import AssistantBridgeProcessor
from voice.pipecat_services import ProviderBridgeSTTService, ProviderBridgeTTSService

logger = logging.getLogger("tars.voice.pipeline")

TTS_OUTPUT_SAMPLE_RATE = 24000  # Kokoro's native rate; Fish Speech/mock resample to it downstream.


def build_voice_pipeline(
    websocket: WebSocket,
    stt_provider: SpeechToTextProvider,
    tts_provider: TextToSpeechProvider,
    assistant_router: AssistantRouter,
    conversation_id: str,
    action_runtime: ActionRuntime | None = None,
) -> PipelineWorker:
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=stt_provider.sample_rate,
            audio_out_sample_rate=TTS_OUTPUT_SAMPLE_RATE,
            add_wav_header=False,
        ),
    )

    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer())
    stt = ProviderBridgeSTTService(provider=stt_provider)
    bridge = AssistantBridgeProcessor(
        assistant_router=assistant_router,
        conversation_id=conversation_id,
        action_runtime=action_runtime,
    )
    tts = ProviderBridgeTTSService(provider=tts_provider, sample_rate=TTS_OUTPUT_SAMPLE_RATE)

    pipeline = Pipeline(
        [
            transport.input(),
            vad,
            stt,
            bridge,
            tts,
            transport.output(),
        ]
    )

    return PipelineWorker(pipeline, params=PipelineParams(), name=f"tars-voice-{conversation_id}")


async def run_voice_session(
    websocket: WebSocket,
    stt_provider: SpeechToTextProvider,
    tts_provider: TextToSpeechProvider,
    assistant_router: AssistantRouter,
    conversation_id: str,
    action_runtime: ActionRuntime | None = None,
) -> None:
    """Runs one voice session to completion (until the client disconnects or
    the pipeline ends). Intended to be awaited from inside the FastAPI
    WebSocket route handler, after `websocket.accept()`-equivalent setup —
    `FastAPIWebsocketTransport` manages the accept/close handshake itself."""
    worker = build_voice_pipeline(
        websocket, stt_provider, tts_provider, assistant_router, conversation_id, action_runtime
    )
    runner = WorkerRunner()
    await runner.add_workers(worker)
    await runner.run()
