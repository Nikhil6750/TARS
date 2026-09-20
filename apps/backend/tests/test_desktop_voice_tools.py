from __future__ import annotations

import time
from types import SimpleNamespace

from app.action_contracts import ActionResult, ActionStatus, RiskLevel
from voice.desktop_tools import DesktopTools
from voice.gemini_live import DEFAULT_VOICE, GeminiLiveVoiceSession, TarsTools, resolve_trading_terms


class Runtime:
    """Stands in for ActionRuntime: records requests, returns scripted results."""

    def __init__(self, script=None):
        self.requests, self.confirms, self.script = [], [], script or {}

    async def submit(self, request):
        self.requests.append(request)
        status, data, error = self.script.get((request.skill, request.action), (ActionStatus.SUCCEEDED, {}, None))
        return ActionResult(request_id=request.id, status=status, risk_level=RiskLevel.LOW_RISK,
                            summary=f"{request.skill}.{request.action}", data={**data}, error=error)

    async def confirm(self, request_id, token, approved):
        self.confirms.append((request_id, token, approved))
        return ActionResult(request_id=request_id, status=ActionStatus.SUCCEEDED if approved else ActionStatus.DENIED,
                            summary="confirmed" if approved else "denied")


def tools(script=None, heard=("", 0.0)):
    rt = Runtime(script)
    box = {"heard": heard}
    return DesktopTools(SimpleNamespace(action_runtime=rt), lambda: box["heard"]), rt, box


CONFIRM = (ActionStatus.CONFIRMATION_REQUIRED, {"confirmation_token": "SECRET"}, None)


async def test_open_app_goes_through_action_runtime_and_reports_done():
    t, rt, _ = tools()
    out = await t.desktop_open_app("notepad")
    assert out["status"] == "DONE"
    req = rt.requests[0]
    assert (req.skill, req.action, req.arguments) == ("windows_app", "launch", {"target": "notepad"})
    assert t.recent[-1]["result"] == "DONE"


async def test_outcomes_are_truthful_not_found_blocked_failed():
    t, _, _ = tools({("windows_app", "focus"): (ActionStatus.FAILED, {}, "No running window found for x"),
                     ("terminal", "run_command"): (ActionStatus.BLOCKED, {}, "blocked"),
                     ("browser", "open_url"): (ActionStatus.FAILED, {}, "bridge exploded")})
    assert (await t.desktop_focus_window("x"))["status"] == "NOT_FOUND"
    assert (await t.run_terminal("Get-Process"))["status"] == "BLOCKED"
    assert (await t.browser_open_url("https://a.b"))["status"] == "FAILED"


async def test_confirmation_token_never_reaches_the_model_and_needs_the_humans_yes():
    t, rt, box = tools({("desktop_control", "invoke_control"): CONFIRM})
    out = await t.desktop_click_control("btn1", "Save")
    assert out["status"] == "NEEDS_CONFIRMATION" and "SECRET" not in str(out)
    # the model tries to confirm before the human has answered
    assert (await t.confirm_pending_action())["status"] == "NEEDS_CONFIRMATION" and not rt.confirms
    box["heard"] = ("hmm what does that do", time.monotonic())
    assert (await t.confirm_pending_action())["status"] == "NEEDS_CONFIRMATION" and not rt.confirms
    box["heard"] = ("no do not do it", time.monotonic())
    assert (await t.confirm_pending_action())["status"] == "NEEDS_CONFIRMATION" and not rt.confirms
    box["heard"] = ("yes go ahead", time.monotonic())
    done = await t.confirm_pending_action()
    assert done["status"] == "DONE" and rt.confirms[0][1] == "SECRET" and rt.confirms[0][2] is True
    assert (await t.confirm_pending_action())["status"] == "NOT_FOUND"  # cannot be replayed


async def test_a_yes_spoken_before_the_request_does_not_count():
    t, rt, _ = tools({("desktop_control", "type_into_control"): CONFIRM}, heard=("yes", time.monotonic() - 5))
    await t.desktop_type_text("c1", "hello", "Notes")
    assert (await t.confirm_pending_action())["status"] == "NEEDS_CONFIRMATION" and not rt.confirms


async def test_cancel_denies_the_pending_action():
    t, rt, _ = tools({("desktop_control", "invoke_control"): CONFIRM})
    await t.desktop_click_control("b", "OK")
    assert (await t.cancel_pending_action())["status"] == "DONE"
    assert rt.confirms and rt.confirms[0][2] is False and t.pending is None


async def test_mt5_order_controls_are_blocked_before_reaching_the_runtime():
    t, rt, _ = tools({("desktop_control", "inspect_current_window"): (
        ActionStatus.SUCCEEDED, {"window": "MetaTrader 5 - Demo terminal64.exe"}, None)})
    for label in ("Buy", "Sell by Market", "New Order", "Close Position", "Modify"):
        out = await t.desktop_click_control("c", label)
        assert out["status"] == "BLOCKED" and "read-only" in out["summary"]
    assert not any(r.action == "invoke_control" for r in rt.requests)
    ok = await t.desktop_click_control("tab", "Positions")  # harmless navigation still goes to the runtime
    assert any(r.action == "invoke_control" for r in rt.requests) and ok["status"] == "DONE"


async def test_order_placement_via_terminal_is_blocked():
    t, rt, _ = tools()
    for cmd in ("python -c 'import MetaTrader5 as m; m.order_send({})'", "mt5.buy('EURUSD')"):
        assert (await t.run_terminal(cmd))["status"] == "BLOCKED"
    assert not rt.requests


async def test_desktop_context_is_lazy_cached_and_lists_recent_actions():
    t, rt, _ = tools({("desktop_control", "inspect_current_window"): (ActionStatus.SUCCEEDED, {"window": "TradingView"}, None)})
    await t.desktop_open_app("chrome")
    a = await t.desktop_context()
    b = await t.desktop_context()
    assert sum(1 for r in rt.requests if r.action == "inspect_current_window") == 1
    assert a["active_window"]["data"]["window"] == "TradingView" and b["recent_actions"][0]["action"] == "open chrome"


async def test_tars_tools_route_desktop_names_and_drop_unknown_args():
    state = SimpleNamespace(action_runtime=Runtime())
    tt = TarsTools(state, "s")
    out = await tt.call("desktop_open_app", {"target": "notepad", "evil": "x"})
    assert out["status"] == "DONE" and state.action_runtime.requests[0].arguments == {"target": "notepad"}
    assert "error" in await tt.call("place_order", {})


def test_ticker_resolution_is_contextual_and_conservative():
    assert resolve_trading_terms("What is happening with The Rusty?") == "What is happening with EURUSD?"
    assert resolve_trading_terms("check the you are USD chart") == "check EURUSD chart"
    assert resolve_trading_terms("Stop. What about gold?") == "Stop. What about gold?"
    assert resolve_trading_terms("blah blah") == "blah blah"


def test_default_voice_is_sadaltager_and_configurable():
    from app.config import Settings

    assert DEFAULT_VOICE == "Sadaltager"
    assert Settings(_env_file=None).gemini_live_voice == "Sadaltager"
    assert Settings(_env_file=None, gemini_live_voice="Puck").gemini_live_voice == "Puck"
    assert GeminiLiveVoiceSession(SimpleNamespace(), lambda e: None, lambda p: False, voice="Achird").voice == "Achird"
    assert GeminiLiveVoiceSession(SimpleNamespace(), lambda e: None, lambda p: False).voice == "Sadaltager"
