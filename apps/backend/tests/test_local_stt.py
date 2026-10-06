"""Focused tests for the local Faster-Whisper STT path: provider/model config, CPU fallback,
load failure, health, normalisation, local intent routing, offline guard, no-cloud basics."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from voice.errors import VoiceProviderError
from voice.interfaces import SynthesisResult, TranscriptionResult
from voice.local_intents import LocalIntentRouter
from voice.providers.faster_whisper_stt import FasterWhisperSTTProvider
from voice.session import VoiceSessionController
from voice.stt_runtime import (
    OFFLINE_REASONING_MESSAGE,
    ConnectivityMonitor,
    STTState,
    detect_backend,
    normalize_stt_text,
    resolve_model_dir,
    resolve_model_name,
)


class FakeModel:
    def __init__(self, name, device, compute_type, **kw):
        self.args = (name, device, compute_type, kw)

    def transcribe(self, samples, **kw):
        self.kw = kw
        return [SimpleNamespace(text=" open calculator ")], SimpleNamespace(language="en")


def test_defaults_and_model_whitelist():
    s = Settings(_env_file=None)
    assert s.faster_whisper_model == "small.en" and s.faster_whisper_device == "auto"
    assert resolve_model_name("medium.en") == "medium.en"
    assert resolve_model_name("large-v3") == "small.en"  # no surprise giant download
    assert resolve_model_name(None) == "small.en"


def test_model_dir_outside_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert resolve_model_dir() == tmp_path / "TARS" / "models" / "whisper"
    assert resolve_model_dir(str(tmp_path / "x")) == tmp_path / "x"


def test_backend_prefers_gpu_only_when_supported(monkeypatch):
    import ctranslate2

    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 1)
    monkeypatch.setattr(ctranslate2, "get_supported_compute_types", lambda d: {"float16", "int8_float16"})
    assert detect_backend("auto") == ("cuda", "int8_float16")
    assert detect_backend("cpu") == ("cpu", "int8")
    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 0)
    assert detect_backend("auto") == ("cpu", "int8")


def test_provider_ready_health_and_beam(tmp_path):
    p = FasterWhisperSTTProvider("small.en", "cpu", model_dir=str(tmp_path), beam_size=1, model_factory=FakeModel)
    h = p.health.snapshot()
    assert h["state"] == STTState.READY.value and h["backend"] == "cpu" and h["model"] == "small.en"
    assert h["offline_capable"] is True and "key" not in str(h).lower()
    r = asyncio.run(p.transcribe(bytes(3200)))
    assert r.text == "open calculator"
    assert p._model.kw["language"] == "en" and p._model.kw["beam_size"] == 1
    assert "TradingView" in p._model.kw["hotwords"]


def test_gpu_failure_falls_back_to_cpu(monkeypatch, tmp_path):
    import ctranslate2

    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 1)
    monkeypatch.setattr(ctranslate2, "get_supported_compute_types", lambda d: {"float16"})

    def factory(name, device, compute_type, **kw):
        if device == "cuda":
            raise RuntimeError("cublas64_12.dll not found")
        return FakeModel(name, device, compute_type, **kw)

    p = FasterWhisperSTTProvider("small.en", "auto", model_dir=str(tmp_path), model_factory=factory)
    assert p.health.backend == "cpu" and p.health.state is STTState.READY
    assert "GPU unavailable" in p.health.detail


def test_gpu_that_loads_but_cannot_decode_falls_back_to_cpu(monkeypatch, tmp_path):
    """Real failure seen on this machine: weights load on CUDA, first decode raises
    'Library cublas64_12.dll is not found'."""
    import ctranslate2

    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 1)
    monkeypatch.setattr(ctranslate2, "get_supported_compute_types", lambda d: {"float16"})

    class CudaBroken(FakeModel):
        def transcribe(self, samples, **kw):
            if self.args[1] == "cuda":
                raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
            return super().transcribe(samples, **kw)

    p = FasterWhisperSTTProvider("small.en", "auto", model_dir=str(tmp_path), model_factory=CudaBroken)
    assert p.health.backend == "cpu" and p.health.state is STTState.READY
    assert asyncio.run(p.transcribe(bytes(3200))).text == "open calculator"


def test_model_load_failure_is_reported_not_silent(tmp_path):
    def factory(*a, **k):
        raise OSError("no model")

    with pytest.raises(VoiceProviderError):
        FasterWhisperSTTProvider("small.en", "cpu", model_dir=str(tmp_path), model_factory=factory)


@pytest.mark.parametrize("raw,expected", [
    ("show X A U U S D on 15 minutes", "show XAUUSD on 15 minutes"),
    ("open trading view", "open TradingView"),
    ("open power shell", "open PowerShell"),
    ("show E U R U S D", "show EURUSD"),
    ("X Y Z A B C stays", "X Y Z A B C stays"),  # unknown spelled word is never merged
    ("open calculator", "open calculator"),
])
def test_normalisation_is_conservative(raw, expected):
    assert normalize_stt_text(raw) == expected


@pytest.mark.parametrize("phrase,name,cloud", [
    ("Open Calculator", "open_app", False),
    ("Open Start and scroll down", "open_start_scroll", False),
    ("Scroll up", "scroll", False),
    ("Watch gold", "watch", False),
    ("Show XAUUSD on 15 minutes.", "tradingview_navigate", False),
    ("Calculate 2345 times 17", "calculate", False),
    ("Pause market monitoring", "pause_monitoring", False),
    ("Resume market monitoring", "resume_monitoring", False),
    ("What is on my screen?", "screen_context", False),
    ("Analyze EURUSD on 5 minutes", "market_analysis", True),
    ("Explain why gold moved today", "market_analysis", True),
])
def test_local_intent_routing(phrase, name, cloud):
    intent = LocalIntentRouter().route(phrase)
    assert intent is not None and intent.name == name and intent.needs_cloud is cloud


def test_tradingview_navigation_steps():
    intent = LocalIntentRouter().route("Open TradingView and show XAUUSD on 15 minutes.")
    assert [s[0] for s in intent.steps] == ["desktop_open_app", "tradingview_set_symbol", "tradingview_set_timeframe"]
    assert intent.steps[1][1] == {"symbol": "XAUUSD"} and intent.steps[2][1] == {"timeframe": "15m"}
    assert LocalIntentRouter().route("calculate 2345 times 17").steps[0][1] == {"expression": "2345 * 17"}
    assert LocalIntentRouter().route("tell me a joke") is None


def test_connectivity_force_offline_and_cache():
    calls = []
    m = ConnectivityMonitor(probe=lambda: calls.append(1) or True)
    assert m.online() and m.online() and len(calls) == 1
    assert ConnectivityMonitor(force_offline=True, probe=lambda: True).online() is False


# ---- session wiring: local commands and offline guard, with no cloud calls ----

class _STT:
    name = "fw"

    async def transcribe(self, _):
        return TranscriptionResult("x")


class _TTS:
    name = "t"

    async def synthesize(self, text):
        return SynthesisResult(b"wav", 24000)


class _Tools:
    def __init__(self):
        self.calls = []
        outer = self
        self.desktop = SimpleNamespace(pending=None, ui_confirm=lambda ok: outer._confirm(ok))

    async def _confirm(self, ok):
        self.calls.append(("ui_confirm", ok))
        return {"status": "DONE" if ok else "CANCELLED", "summary": "done"}

    async def call(self, name, args):
        self.calls.append((name, args))
        return {"status": "DONE", "summary": f"{name} ok"}


class _Turns:
    def __init__(self, needs_online=True):
        self.streamed = []
        self.needs_online = needs_online

    def requires_online(self, text):
        return self.needs_online

    async def stream_text(self, text, **kw):
        self.streamed.append(text)
        yield SimpleNamespace(type="complete")

    async def cancel_turn(self, t): ...


def _session(tools, turns, online):
    voice = SimpleNamespace(stt=_STT(), tts=_TTS())
    conn = ConnectivityMonitor(force_offline=not online, probe=lambda: True)
    return VoiceSessionController(turns, voice, lambda e: asyncio.sleep(0), lambda b: False,
                                  local_tools=tools, connectivity=conn)


async def _events(session, text):
    return [e async for e in session._turn_events(text, "", "t1")]


async def test_offline_local_command_runs_without_cloud():
    tools, turns = _Tools(), _Turns()
    s = _session(tools, turns, online=False)
    events = await _events(s, "Open TradingView and show XAUUSD on 15 minutes")
    assert [c[0] for c in tools.calls] == ["desktop_open_app", "tradingview_set_symbol", "tradingview_set_timeframe"]
    assert events[-1].response.provider == "local_intent" and "XAUUSD on 15m" in events[-1].response.speech_text
    assert turns.streamed == [] and s.cloud_calls == 0


async def test_offline_reasoning_request_is_truthful_and_makes_no_cloud_call():
    tools, turns = _Tools(), _Turns()
    s = _session(tools, turns, online=False)
    for phrase in ("Explain why gold moved today", "Tell me about the weather philosophy"):
        events = await _events(s, phrase)
        assert events[-1].response.speech_text == OFFLINE_REASONING_MESSAGE
    assert turns.streamed == [] and s.cloud_calls == 0 and tools.calls == []


async def test_online_unmatched_text_goes_to_cloud_path_and_local_text_never_does():
    tools, turns = _Tools(), _Turns()
    s = _session(tools, turns, online=True)
    await _events(s, "Tell me about the weather philosophy")
    assert turns.streamed and s.cloud_calls == 1
    await _events(s, "Open Calculator")
    assert s.cloud_calls == 1 and s.local_commands == 1


async def test_failed_step_stops_chain_and_is_reported():
    class Fail(_Tools):
        async def call(self, name, args):
            self.calls.append((name, args))
            return {"status": "FAILED", "summary": "TradingView is not running"}

    tools = Fail()
    s = _session(tools, _Turns(), online=False)
    events = await _events(s, "Open TradingView and show XAUUSD on 15 minutes")
    assert len(tools.calls) == 1 and "not running" in events[-1].response.speech_text


async def test_pending_confirmation_yes_no_is_handled_locally():
    tools = _Tools()
    tools.desktop.pending = {"id": "1"}
    s = _session(tools, _Turns(), online=False)
    await _events(s, "yes")
    assert tools.calls[-1] == ("ui_confirm", True)
    await _events(s, "no")
    assert tools.calls[-1] == ("ui_confirm", False)


def test_faster_whisper_is_offline_lane_only():
    """Online/auto: Gemini Live owns the mic. Faster-Whisper is used only when offline/forced/unavailable."""
    src = Path(__file__).resolve().parents[1].joinpath("app/routers/realtime.py").read_text(encoding="utf-8")
    assert 'use_gemini = gemini_ok and mode != "offline"' in src
    assert "stt_provider" not in src  # STT_PROVIDER no longer picks the lane


# ---- native laptop control + system intents ----

@pytest.mark.parametrize("phrase,action,args", [
    ("Set volume to 30 percent", "set_volume", {"percent": 30}),
    ("Increase volume by 10 percent", "volume_up", {"step": 10}),
    ("Brightness down", "brightness_down", {}),
    ("Mute", "mute_audio", {}),
    ("Unmute", "unmute_audio", {}),
    ("Mute microphone", "mute_microphone", {}),
    ("Pause", "media_play_pause", {}),
    ("Next track", "media_next", {}),
    ("Restart the laptop", "restart", {}),
])
def test_system_intents(phrase, action, args):
    intent = LocalIntentRouter().route(phrase)
    assert intent.steps == [("system_control", {"action": action, **args})]


def test_compound_intents_route_every_clause_or_nothing():
    r = LocalIntentRouter()
    assert [s[0] for s in r.route("Open Calculator and calculate 2345 times 17").steps] == ["calculator_calculate"]
    assert [s[0] for s in r.route("Set volume to 30 and open YouTube Music").steps] == ["system_control", "desktop_open_app"]
    assert r.route("Open Settings and go to Bluetooth") is None  # unknown clause -> not guessed


def test_windows_system_risk_and_native_contract(monkeypatch):
    from actions.permissions import PermissionEngine
    from app.action_contracts import ActionStatus, RiskLevel
    from skills import windows_system as ws

    engine, skill = PermissionEngine(), ws.WindowsSystemSkill()
    for ok in ("set_volume", "mute_audio", "set_brightness", "media_next", "window_switch"):
        assert engine.classify(skill, ok, {"percent": 30}) is RiskLevel.LOW_RISK
    for power in ("shutdown", "restart", "sleep", "window_close"):
        assert engine.classify(skill, power, {}) is RiskLevel.CONFIRM_REQUIRED

    state = {"vol": 50, "muted": True}
    monkeypatch.setattr(ws, "_get_volume", lambda: (state["vol"], state["muted"]))
    monkeypatch.setattr(ws, "_set_volume", lambda p: state.update(vol=p))
    monkeypatch.setattr(ws, "_set_mute", lambda m: state.update(muted=m))
    status, summary, data = skill._run("volume_up", {"step": 10})
    assert status is ActionStatus.SUCCEEDED and state["vol"] == 60 and not state["muted"] and data["verified"]
    monkeypatch.setattr(ws, "_set_volume", lambda p: None)  # driver ignores the write -> not claimed as done
    status, *_ = skill._run("set_volume", {"percent": 10})
    assert status is ActionStatus.FAILED
    monkeypatch.setattr(ws, "_get_brightness", lambda: None)
    status, summary, _ = skill._run("set_brightness", {"percent": 50})
    assert status is ActionStatus.FAILED and "UNSUPPORTED" in summary
    with pytest.raises(Exception):
        asyncio.run(skill.validate("set_volume", {"percent": 150}))


def test_uwp_corewindow_activates_its_application_frame(monkeypatch):
    """Regression: Calculator/Settings resolve to an inner CoreWindow; activating/verifying that handle
    never reached the foreground. The owning ApplicationFrameWindow must be the target."""
    from skills import windows_app as wa

    classes = {10: "Windows.UI.Core.CoreWindow", 20: "ApplicationFrameWindow", 30: "ApplicationFrameWindow"}
    titles = {10: "Calculator", 20: "Calculator", 30: "Other"}
    monkeypatch.setattr(wa.win32gui, "GetClassName", lambda h: classes[h])
    monkeypatch.setattr(wa.win32gui, "GetWindowText", lambda h: titles[h])
    monkeypatch.setattr(wa.win32gui, "IsWindowVisible", lambda h: True)
    monkeypatch.setattr(wa.win32gui, "EnumWindows", lambda cb, extra: [cb(h, extra) for h in (30, 20)])
    assert wa._activation_target(10) == 20
    assert wa._activation_target(20) == 20  # ordinary windows are untouched
