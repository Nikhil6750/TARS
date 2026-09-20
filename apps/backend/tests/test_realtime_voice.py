from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from assistant.turn_controller import AssistantResponse, TurnEvent, TurnIntent, TurnStatus
from voice.interfaces import SynthesisResult, TranscriptionResult
from voice.session import LatencyMetrics, VoiceSessionController, VoiceState
from voice.streaming import IncrementalWhisperSTT, LocalStreamingTTS, SpeechChunker


class STT:
    name = "test_local"

    async def transcribe(self, _pcm):
        return TranscriptionResult("What about gold?")


class TTS:
    name = "test_local"

    def __init__(self):
        self.spoken = []
        self.block = None

    async def synthesize(self, text):
        self.spoken.append(text)
        if self.block:
            await self.block.wait()
        return SynthesisResult(b"wav", 24000)


class Turns:
    def __init__(self):
        self.cancelled = []
        self.release = asyncio.Event()
        self.finish = asyncio.Event()

    async def cancel_turn(self, turn):
        self.cancelled.append(turn)

    async def stream_text(self, text, **kwargs):
        turn = kwargs["turn_id"]
        yield TurnEvent(turn_id=turn, type="delta", text="First complete sentence. ")
        self.release.set()
        await self.finish.wait()
        yield TurnEvent(turn_id=turn, type="delta", text="Second sentence.")
        yield TurnEvent(turn_id=turn, type="complete", response=AssistantResponse(
            turn_id=turn, display_text="First complete sentence. Second sentence.",
            speech_text="First complete sentence. Second sentence.",
            intent=TurnIntent.NORMAL_CONVERSATION, status=TurnStatus.COMPLETED,
            provider="test_local", latency_ms=1, conversation_id="test"))


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.fixture
async def session():
    events = []
    async def emit(event):
        events.append(event)
    turns, tts = Turns(), TTS()
    controller = VoiceSessionController(turns, SimpleNamespace(stt=STT(), tts=tts),
                                        emit, lambda pcm: pcm[0] != 0)
    await controller.start()
    yield controller, events, turns, tts
    await controller.close()


async def start_turn(session, utterance=1):
    await session.on_stt({"type": "speech_started", "utterance": utterance})
    await session.on_stt({"type": "speech_ended", "utterance": utterance, "at": time.perf_counter()})
    await session.on_stt({"type": "final_transcript", "utterance": utterance, "text": "What about gold?"})


async def test_stream_speaks_before_assistant_finishes_and_playback_owns_completion(session):
    controller, events, turns, tts = session
    await start_turn(controller)
    await eventually(lambda: any(e["type"] == "audio_chunk" for e in events))
    assert not turns.finish.is_set()
    assert tts.spoken == ["First complete sentence."]
    assert controller.state is VoiceState.ASSISTANT_SPEAKING
    turns.finish.set()
    await eventually(lambda: controller.tts_done)
    assert controller.state is VoiceState.ASSISTANT_SPEAKING
    for event in list(events):
        if event["type"] == "audio_chunk":
            await controller.playback({**event, "type": "audio_started"})
            await controller.playback({**event, "type": "audio_done"})
    assert controller.state is VoiceState.LISTENING
    assert "speech_ended_to_first_audible_audio" in controller.metrics.latest


async def test_barge_in_cancels_old_generation_and_new_turn_speaks(session):
    controller, events, turns, _tts = session
    await start_turn(controller)
    await eventually(lambda: bool(controller.audio_pending))
    old = controller.turn_id
    await controller.on_stt({"type": "speech_started", "utterance": 2})
    boundary = len(events)
    assert controller.state is VoiceState.USER_SPEAKING
    assert not controller.audio_pending
    assert old in turns.cancelled
    turns.finish.set()
    await asyncio.sleep(0.01)
    assert not any(e["type"] == "audio_chunk" and e["turn_id"] == old for e in events[boundary:])
    await controller.playback({"type": "audio_done", "turn_id": old, "sequence": 1})
    assert controller.state is VoiceState.USER_SPEAKING
    await controller.on_stt({"type": "final_transcript", "utterance": 2, "text": "Gold?"})
    await eventually(lambda: any(e["type"] == "audio_chunk" and e["turn_id"] != old for e in events[boundary:]))
    await controller.playback({"type": "playback_stopped", "turn_id": old})
    assert "barge_in_detected_to_playback_stopped" in controller.metrics.latest


async def test_late_final_cannot_take_new_turn(session):
    controller, events, _, _ = session
    await controller.on_stt({"type": "speech_started", "utterance": 1})
    await controller.on_stt({"type": "speech_started", "utterance": 2})
    await controller.on_stt({"type": "final_transcript", "utterance": 1, "text": "stale"})
    assert not any(e["type"] == "final_transcript" for e in events)
    assert controller.state is VoiceState.USER_SPEAKING


async def test_cancelled_inference_can_never_publish_audio():
    entered, release = asyncio.Event(), asyncio.Event()
    class UncooperativeTTS(TTS):
        async def synthesize(self, text):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return SynthesisResult(b"late", 24000)
    audio, completed = [], []
    async def on_audio(chunk):
        audio.append(chunk)
    async def on_complete():
        completed.append(True)
    async def text():
        yield "Old answer."
    stream = LocalStreamingTTS(UncooperativeTTS(), on_audio, on_complete)
    task = asyncio.create_task(stream.start(text()))
    await entered.wait()
    stream.flush()
    release.set()
    await asyncio.gather(task, return_exceptions=True)
    assert audio == [] and completed == []


async def test_partial_final_order_and_adaptive_short_pause():
    events = []
    async def emit(event):
        events.append(event)
    stt = IncrementalWhisperSTT(STT(), emit, lambda pcm: pcm[0] != 0, partial_interval=0.1)
    await stt.start()
    try:
        for _ in range(24):
            await stt.push_audio(b"\x01\x00" * 512)
            await asyncio.sleep(0.002)
        assert any(e["type"] == "partial_transcript" for e in events)
        for _ in range(6):
            await stt.push_audio(bytes(1024))
        assert stt.active  # Natural 192ms pause must not finalize.
        for _ in range(5):
            await stt.push_audio(b"\x01\x00" * 512)
        for _ in range(13):
            await stt.push_audio(bytes(1024))
        await eventually(lambda: any(e["type"] == "final_transcript" for e in events))
        kinds = [e["type"] for e in events]
        assert kinds.index("speech_started") < kinds.index("partial_transcript") < kinds.index("speech_ended") < kinds.index("final_transcript")
        assert kinds.count("final_transcript") == 1
    finally:
        await stt.stop()


async def test_tts_failure_does_not_block_response_or_next_turn(session):
    controller, events, turns, tts = session
    async def fail(_text):
        raise RuntimeError("device absent")
    tts.synthesize = fail
    turns.finish.set()
    await start_turn(controller)
    await eventually(lambda: controller.response_task.done())
    assert controller.provider_status["tts"] == "ERROR"
    assert any(e["type"] == "response_complete" for e in events)
    await controller.on_stt({"type": "speech_started", "utterance": 2})
    assert controller.state is VoiceState.USER_SPEAKING


async def test_audio_backpressure_bounded_and_cancel_unblocks(session):
    controller, _, turns, _ = session
    turns.finish.set()
    await start_turn(controller)
    await eventually(lambda: len(controller.audio_pending) == 2)
    await controller.interrupt()
    assert not controller.audio_pending
    assert controller.audio_credit._value == 2


@pytest.mark.parametrize("pieces", [
    ["Price is 1.", "2345. ", "Next."],
    ["**Price** is 1.2345. ", "```py\nsecret()", "\n``` Next."],
    ["Price is 1.2345. Visit https://example.com/path. ", "Next."],
])
def test_speech_chunks_withhold_broken_numbers_markup_urls_and_code(pieces):
    chunker = SpeechChunker()
    result = []
    for text in pieces:
        result.extend(chunker.feed(text))
    result.extend(chunker.feed("", final=True))
    spoken = " ".join(result)
    assert "1.2345" in spoken
    assert all(value not in spoken for value in ["secret", "http", "example.com", "*", "`"])
    assert "1." not in result


def test_metrics_only_publish_p50_after_repeated_measurements():
    metrics = LatencyMetrics()
    metrics.mark("one", "speech_ended", 1)
    metrics.mark("one", "final_transcript", 1.1)
    assert metrics.snapshot()["p50_ms"] == {}
    metrics.mark("one", "final_transcript", 9)  # duplicate ACK cannot inflate samples
    metrics.mark("two", "speech_ended", 2)
    metrics.mark("two", "final_transcript", 2.3)
    assert metrics.snapshot()["p50_ms"]["speech_ended_to_final_transcript"] == 200


async def test_proactive_speech_never_interrupts_user(session):
    controller, events, _, _ = session
    await controller.on_stt({"type": "speech_started", "utterance": 1})
    await controller.speak_alert("Critical alert")
    assert not any(e["type"] == "audio_chunk" for e in events)


def test_duplex_websocket_partial_final_barge_in_and_disconnect(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import realtime

    monkeypatch.setattr(realtime, "SileroStreamingVAD", lambda: lambda pcm: pcm[0] != 0)
    monkeypatch.setattr(realtime.SherpaPartialEngine, "available", classmethod(lambda cls, d: False))
    app = FastAPI()
    ready = asyncio.Event()
    ready.set()
    app.state.voice_providers = SimpleNamespace(stt=STT(), tts=TTS(), ready=ready)
    app.state.turn_controller = Turns()
    app.include_router(realtime.router)
    def receive_until(ws, kind):
        for _ in range(100):
            item = ws.receive_json()
            if item["type"] == kind:
                return item
        raise AssertionError(f"missing {kind}")
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/voice/realtime") as ws:
            for _ in range(25):
                ws.send_bytes(b"\x01\x00" * 512)
            partial = receive_until(ws, "partial_transcript")
            assert partial["text"]
            for _ in range(24):
                ws.send_bytes(bytes(1024))
            final = receive_until(ws, "final_transcript")
            audio = receive_until(ws, "audio_chunk")
            assert final["turn_id"] == audio["turn_id"] == partial["turn_id"]
            for _ in range(4):
                ws.send_bytes(b"\x01\x00" * 512)
            interrupt = receive_until(ws, "interrupt")
            assert interrupt["previous_turn_id"] == audio["turn_id"]
            assert interrupt["turn_id"] != audio["turn_id"]
        assert app.state.realtime_session is None


def test_streaming_partial_engine_emits_live_partials_and_whisper_final():
    class Engine:
        def __init__(self):
            self.texts = iter(["Tars", "Tars what", "Tars what is", "Tars what is", "Tars what is", "Tars what is", "Tars what is"])
            self.resets = 0

        def reset(self):
            self.resets += 1

        def feed(self, pcm):
            return next(self.texts, "Tars what is")

    async def scenario():
        events = []

        async def emit(ev):
            events.append(ev["type"])

        from voice.streaming import IncrementalWhisperSTT
        engine = Engine()
        stt = IncrementalWhisperSTT(STT(), emit, lambda pcm: pcm[0] != 0, partial_engine=engine)
        await stt.start()
        for _ in range(12):
            await stt.push_audio(bytes([1, 0]) * 512)
        for _ in range(30):
            await stt.push_audio(bytes(1024))
        await asyncio.sleep(0.2)
        await stt.stop()
        return events, engine.resets

    events, resets = asyncio.run(scenario())
    assert "partial_transcript" in events and "final_transcript" in events
    assert events.index("partial_transcript") < events.index("speech_ended") < events.index("final_transcript")
    assert resets == 1
