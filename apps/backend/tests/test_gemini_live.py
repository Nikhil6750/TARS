from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path
from types import SimpleNamespace

from voice.gemini_live import TOOL_NAMES, GeminiLiveVoiceSession, TarsTools
from voice.session import VoiceState


def msg(**kw):
    base = dict(server_content=None, tool_call=None, tool_call_cancellation=None, go_away=None, setup_complete=None)
    base.update(kw)
    return SimpleNamespace(**base)


def content(**kw):
    base = dict(interrupted=None, input_transcription=None, output_transcription=None, model_turn=None,
                turn_complete=None)
    base.update(kw)
    return SimpleNamespace(**base)


def audio_part(data=b"\x01\x00" * 100):
    return SimpleNamespace(inline_data=SimpleNamespace(data=data))


class FakeLive:
    def __init__(self):
        self.script: asyncio.Queue = asyncio.Queue()
        self.sent_audio = 0
        self.tool_responses = []
        self.texts = []

    async def send_realtime_input(self, audio=None, text=None, **kw):
        if audio is not None:
            self.sent_audio += 1
        if text:
            self.texts.append(text)

    async def send_tool_response(self, function_responses):
        self.tool_responses.extend(function_responses)

    async def receive(self):
        while True:
            item = await self.script.get()
            if item is None:
                return
            yield item


class FakeConnect:
    def __init__(self, live=None, fail=None):
        self.live, self.fail, self.count = live or FakeLive(), fail, 0

    def __call__(self):
        outer = self

        @contextlib.asynccontextmanager
        async def cm():
            outer.count += 1
            if outer.fail:
                raise outer.fail
            yield outer.live

        return cm()


class Tools:
    def __init__(self):
        self.calls = []

    async def call(self, name, args):
        self.calls.append((name, args))
        return {"answer": "gold is quiet"} if name == "ask_claude" else {"ok": True}


def make(connect, events, **kw):
    async def emit(ev):
        events.append(ev)

    return GeminiLiveVoiceSession(kw.pop("tools", Tools()), emit, lambda pcm: pcm[0] != 0, connect=connect,
                                  idle_seconds=kw.pop("idle_seconds", 30), **kw)


async def speak(session, frames=12):
    for _ in range(frames):
        await session.push_audio(bytes([1, 0]) * 512)


async def wait_for(pred, timeout=2.0):
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


async def test_session_opens_lazily_on_speech_not_on_silence_and_flushes_preroll():
    events, connect = [], FakeConnect()
    s = make(connect, events)
    await s.start()
    for _ in range(30):
        await s.push_audio(bytes(1024))
    assert connect.count == 0 and s.provider_status["gemini_live"] == "IDLE"
    await speak(s, 12)
    assert await wait_for(lambda: connect.count == 1 and s.provider_status["gemini_live"] == "CONNECTED")
    assert connect.live.sent_audio >= 8  # preroll (the words spoken while connecting) was flushed
    await s.close()


async def test_transcripts_audio_response_and_turn_complete_are_forwarded():
    events, connect = [], FakeConnect()
    s = make(connect, events)
    await s.start()
    await speak(s)
    await wait_for(lambda: connect.count == 1)
    q = connect.live.script
    await q.put(msg(server_content=content(input_transcription=SimpleNamespace(text="TARS, can you "))))
    await q.put(msg(server_content=content(input_transcription=SimpleNamespace(text="hear me?"))))
    await q.put(msg(server_content=content(model_turn=SimpleNamespace(parts=[audio_part()]))))
    await q.put(msg(server_content=content(output_transcription=SimpleNamespace(text="Loud and clear."))))
    await q.put(msg(server_content=content(turn_complete=True)))
    assert await wait_for(lambda: any(e["type"] == "response_complete" for e in events))
    types = [e["type"] for e in events]
    assert types.index("speech_started") < types.index("partial_transcript") < types.index("final_transcript") \
        < types.index("audio_pcm") < types.index("response_complete")
    assert [e for e in events if e["type"] == "final_transcript"][0]["text"] == "TARS, can you hear me?"
    done = [e for e in events if e["type"] == "response_complete"][0]
    assert done["response"]["display_text"] == "Loud and clear."
    assert [e for e in events if e["type"] == "audio_pcm"][0]["sample_rate"] == 24000
    assert s.state is VoiceState.LISTENING
    await s.close()


async def test_native_interruption_bumps_generation_so_stale_audio_is_dropped_client_side():
    events, connect = [], FakeConnect()
    s = make(connect, events)
    await s.start()
    await speak(s)
    await wait_for(lambda: connect.count == 1)
    q = connect.live.script
    await q.put(msg(server_content=content(model_turn=SimpleNamespace(parts=[audio_part()]))))
    await wait_for(lambda: any(e["type"] == "audio_pcm" for e in events))
    first_turn = [e for e in events if e["type"] == "audio_pcm"][0]["turn_id"]
    await q.put(msg(server_content=content(interrupted=True)))
    await q.put(msg(server_content=content(input_transcription=SimpleNamespace(text="Stop. What about gold?"))))
    assert await wait_for(lambda: any(e["type"] == "partial_transcript" for e in events[-3:]))
    interrupt = [e for e in events if e["type"] == "interrupt"][0]
    assert interrupt["previous_turn_id"] == first_turn and interrupt["generation"] == 1
    later = [e for e in events if e["type"] in ("partial_transcript", "speech_started") and e["seq"] > interrupt["seq"]]
    assert later and all(e["generation"] == 1 and e["turn_id"] != first_turn for e in later)
    await s.close()


async def test_tool_call_is_dispatched_and_result_returned_to_gemini():
    events, connect, tools = [], FakeConnect(), Tools()
    s = make(connect, events, tools=tools)
    await s.start()
    await speak(s)
    await wait_for(lambda: connect.count == 1)
    call = SimpleNamespace(id="c1", name="ask_claude", args={"question": "gold outlook?"})
    await connect.live.script.put(msg(tool_call=SimpleNamespace(function_calls=[call])))
    assert await wait_for(lambda: connect.live.tool_responses)
    resp = connect.live.tool_responses[0]
    assert resp.id == "c1" and resp.name == "ask_claude" and resp.response == {"answer": "gold is quiet"}
    assert tools.calls == [("ask_claude", {"question": "gold outlook?"})]
    await s.close()


async def test_tool_cancellation_cancels_running_tool():
    events, connect = [], FakeConnect()

    class SlowTools(Tools):
        async def call(self, name, args):
            await asyncio.sleep(30)

    s = make(connect, events, tools=SlowTools())
    await s.start()
    await speak(s)
    await wait_for(lambda: connect.count == 1)
    call = SimpleNamespace(id="c9", name="ask_claude", args={"question": "x"})
    await connect.live.script.put(msg(tool_call=SimpleNamespace(function_calls=[call])))
    await wait_for(lambda: "c9" in s._tool_tasks)
    await connect.live.script.put(msg(tool_call_cancellation=SimpleNamespace(ids=["c9"])))
    assert await wait_for(lambda: "c9" not in s._tool_tasks)
    assert not connect.live.tool_responses
    await s.close()


async def test_idle_session_is_closed_and_reopens_on_next_speech():
    events, connect = [], FakeConnect()
    s = make(connect, events, idle_seconds=0.05)
    await s.start()
    await speak(s)
    await wait_for(lambda: connect.count == 1)
    assert await wait_for(lambda: s.provider_status["gemini_live"] == "IDLE", timeout=4)
    for _ in range(30):
        await s.push_audio(bytes(1024))
    await speak(s)
    assert await wait_for(lambda: connect.count == 2)
    await s.close()


async def test_connect_failure_is_truthful_and_marks_fatal_for_local_fallback():
    events, connect = [], FakeConnect(fail=OSError("no network"))
    s = make(connect, events)
    await s.start()
    await speak(s)
    assert await wait_for(lambda: s.fatal is not None)
    assert s.provider_status["gemini_live"] == "ERROR"
    assert any("Gemini Live unavailable" in (e.get("detail") or "") for e in events)


def test_tools_are_bounded_and_none_can_trade():
    from voice.desktop_tools import DESKTOP_TOOL_NAMES

    base = {"get_market_context", "get_recent_events", "get_mt5_state", "get_tradingview_state",
            "get_economic_calendar", "ask_claude"}
    assert TOOL_NAMES == base | DESKTOP_TOOL_NAMES
    assert not any(re.search(r"trade|order|buy|sell", n) for n in TOOL_NAMES)
    for name in ("voice/gemini_live.py", "voice/desktop_tools.py"):
        source = (Path(__file__).parents[1] / name).read_text(encoding="utf-8")
        assert not re.search(r"order_send\(|place_order|execute_trade|close_position|\bBuy\(|\bSell\(", source)


async def test_tars_tools_report_missing_sources_truthfully_and_ask_claude_delegates():
    class Monitors:
        replay_calendar = None
        calendar = None

        async def status(self):
            return {"mt5": {"state": "DISCONNECTED", "quotes": {}}, "tradingview": {"state": "NOT FOUND"},
                    "calendar": {"next": None}, "quote": None, "replay": False}

    class Turns:
        async def stream_text(self, text, **kw):
            assert "EURUSD outlook" in text and "mt5=DISCONNECTED" in text
            yield SimpleNamespace(type="complete", response=SimpleNamespace(
                display_text="Claude says range-bound.", status=SimpleNamespace(value="completed")))

    tools = TarsTools(SimpleNamespace(monitors=Monitors(), turn_controller=Turns()), "s1")
    ctx = await tools.call("get_market_context", {"symbol": "eurusd"})
    assert ctx["quote"] is None and "MT5 DISCONNECTED" in ctx["quote_note"]
    assert (await tools.call("ask_claude", {"question": "EURUSD outlook?"}))["answer"] == "Claude says range-bound."
    assert "error" in await tools.call("place_trade", {})
