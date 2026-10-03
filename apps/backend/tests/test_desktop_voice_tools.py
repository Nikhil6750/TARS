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


async def test_resolve_app_and_list_installed_apps_go_through_action_runtime_read_only():
    t, rt, _ = tools({
        ("windows_app", "resolve"): (ActionStatus.SUCCEEDED, {"outcome": "MATCH", "app": {"display_name": "Clock"}}, None),
        ("windows_app", "list_installed"): (ActionStatus.SUCCEEDED, {"apps": ["Calculator", "Clock"]}, None),
    })
    resolved = await t.desktop_resolve_app("clock")
    assert resolved["status"] == "DONE"
    assert rt.requests[0].arguments == {"target": "clock"}
    assert rt.requests[0].action == "resolve"

    listed = await t.desktop_list_installed_apps("cal")
    assert listed["status"] == "DONE"
    assert rt.requests[1].arguments == {"query": "cal"}
    assert rt.requests[1].action == "list_installed"


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


async def test_multiple_pending_confirmations_refuse_to_guess_on_yes():
    """Mission: P0 regression section 8 -- never guess which pending
    confirmation a bare "yes" refers to when more than one is active."""
    t, rt, box = tools({
        ("desktop_control", "invoke_control"): CONFIRM,
        ("desktop_control", "type_into_control"): CONFIRM,
    })
    await t.desktop_click_control("b1", "Save")
    first_pending_id = t.pending["id"]
    await t.desktop_type_text("c1", "2345*17", "Calculator")
    assert len(t._pending_queue) == 2

    box["heard"] = ("yes", time.monotonic())
    out = await t.confirm_pending_action()
    assert out["status"] == "AMBIGUOUS"
    assert not rt.confirms  # never guessed, nothing executed
    assert len(t._pending_queue) == 2  # neither was consumed


async def test_ui_confirm_also_refuses_to_guess_when_multiple_are_pending():
    t, rt, _box = tools({
        ("desktop_control", "invoke_control"): CONFIRM,
        ("desktop_control", "type_into_control"): CONFIRM,
    })
    await t.desktop_click_control("b1", "Save")
    await t.desktop_type_text("c1", "2345*17", "Calculator")

    out = await t.ui_confirm(True)
    assert out["status"] == "AMBIGUOUS"
    assert not rt.confirms


async def test_confirmation_replay_is_reported_as_already_handled_not_failed():
    """Mission: P0 regression section 10 -- UI and voice share one source
    of truth. If the UI already resolved a confirmation (e.g. the user
    clicked Yes) and a stale voice "yes" for the SAME request arrives
    after, that is "already handled," not a confusing FAILED."""
    t, rt, box = tools({("desktop_control", "invoke_control"): CONFIRM})
    await t.desktop_click_control("b1", "Save")

    async def replay_confirm(request_id, token, approved):
        from actions.errors import ConfirmationReplayError
        raise ConfirmationReplayError("Confirmation was already consumed")

    rt.confirm = replay_confirm
    box["heard"] = ("yes", time.monotonic())
    out = await t.confirm_pending_action()
    assert out["status"] == "ALREADY_HANDLED"


async def test_cancel_denies_the_pending_action():
    t, rt, _ = tools({("desktop_control", "invoke_control"): CONFIRM})
    await t.desktop_click_control("b", "OK")
    assert (await t.cancel_pending_action())["status"] == "DONE"
    assert rt.confirms and rt.confirms[0][2] is False and t.pending is None


async def test_mt5_order_controls_are_blocked_before_reaching_the_runtime():
    t, rt, _ = tools({("desktop_control", "inspect_screen"): (
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
    t, rt, _ = tools({("desktop_control", "inspect_screen"): (ActionStatus.SUCCEEDED, {"window": "TradingView"}, None)})
    await t.desktop_open_app("chrome")
    a = await t.desktop_context()
    b = await t.desktop_context()
    assert sum(1 for r in rt.requests if r.action == "inspect_screen") == 1
    assert a["primary_visible_window"]["data"]["window"] == "TradingView" and b["recent_actions"][0]["action"] == "open chrome"


async def test_tars_tools_route_desktop_names_and_drop_unknown_args():
    state = SimpleNamespace(action_runtime=Runtime())
    tt = TarsTools(state, "s")
    out = await tt.call("desktop_open_app", {"target": "notepad", "evil": "x"})
    assert out["status"] == "DONE" and state.action_runtime.requests[0].arguments == {"target": "notepad"}
    assert "error" in await tt.call("place_order", {})


async def test_tars_tools_route_resolve_and_list_installed_apps():
    state = SimpleNamespace(action_runtime=Runtime())
    tt = TarsTools(state, "s")
    out = await tt.call("desktop_resolve_app", {"target": "tradingview"})
    assert out["status"] == "DONE"
    assert state.action_runtime.requests[0].skill == "windows_app"
    assert state.action_runtime.requests[0].action == "resolve"
    out2 = await tt.call("desktop_list_installed_apps", {"query": "code"})
    assert out2["status"] == "DONE"
    assert state.action_runtime.requests[1].action == "list_installed"


def test_no_desktop_tool_declaration_invents_an_executable_name_or_path():
    """Item 1/6: Gemini must never be told to pass an executable name or path -- only a plain
    spoken app name, which TARS itself resolves."""
    from voice.gemini_live import _tool_declarations

    decls = {f.name: f for t in _tool_declarations() for f in t.function_declarations}
    desc = decls["desktop_open_app"].description.lower()
    assert "spoken name" in desc
    assert "never pass a guessed" in desc


# ---- desktop/trading context tracker (mission item 1: survives normal voice turns) ------------

async def test_opening_tradingview_sets_current_trading_app():
    t, rt, _ = tools()
    out = await t.desktop_open_app("tradingview")
    assert out["status"] == "DONE"
    assert t.current_trading_app == "TradingView"
    assert t.last_target_app == "tradingview"


async def test_opening_mt5_alias_sets_current_trading_app():
    t, rt, _ = tools()
    await t.desktop_open_app("mt5")
    assert t.current_trading_app == "MetaTrader 5"


async def test_opening_an_unrelated_app_does_not_touch_trading_context():
    t, rt, _ = tools()
    await t.desktop_open_app("calculator")
    assert t.current_trading_app is None
    assert t.last_target_app == "calculator"


async def test_failed_open_does_not_update_context():
    t, rt, _ = tools({("windows_app", "launch"): (ActionStatus.FAILED, {}, "not found: no installed application matches 'x'")})
    await t.desktop_open_app("nonexistent app")
    assert t.last_target_app is None
    assert t.current_trading_app is None


async def test_focus_window_also_sets_trading_context():
    t, rt, _ = tools()
    await t.desktop_focus_window("TradingView")
    assert t.current_trading_app == "TradingView"


async def test_tradingview_set_symbol_updates_context_and_survives_a_followup():
    """The acceptance scenario: open TradingView, then a bare "switch to EURUSD" / "make it
    fifteen minutes" without re-naming the app -- desktop_context() must reflect it afterward."""
    t, rt, _ = tools({
        ("tradingview", "set_symbol"): (ActionStatus.SUCCEEDED, {"outcome": "SUCCESS", "symbol": "EURUSD"}, None),
        ("tradingview", "set_timeframe"): (ActionStatus.SUCCEEDED, {"outcome": "SUCCESS", "symbol": "EURUSD", "timeframe": "15m"}, None),
        ("desktop_control", "inspect_screen"): (ActionStatus.SUCCEEDED, {"window": "TradingView"}, None),
    })
    await t.desktop_open_app("tradingview")
    out1 = await t.tradingview_set_symbol("EURUSD")
    assert out1["status"] == "DONE"
    assert t.current_symbol == "EURUSD"

    out2 = await t.tradingview_set_timeframe("15m")
    assert out2["status"] == "DONE"
    assert t.current_timeframe == "15m"

    ctx = await t.desktop_context()
    assert ctx["previously_opened_by_tars"]["current_trading_app"] == "TradingView"
    assert ctx["previously_opened_by_tars"]["current_symbol"] == "EURUSD"
    assert ctx["previously_opened_by_tars"]["current_timeframe"] == "15m"


async def test_tradingview_status_syncs_context_without_changing_anything():
    t, rt, _ = tools({("tradingview", "status"): (ActionStatus.SUCCEEDED, {"symbol": "XAUUSD", "timeframe": "1h"}, None)})
    out = await t.tradingview_status()
    assert out["status"] == "DONE"
    assert t.current_symbol == "XAUUSD" and t.current_timeframe == "1h"
    assert rt.requests[0].action == "status"


# ---- watch_this_chart: explicit background-watch acknowledgement, never analyze_chart's path ----

async def test_watch_this_chart_acknowledges_immediately_without_analyzing():
    t, rt, _ = tools({("tradingview", "status"): (ActionStatus.SUCCEEDED, {"symbol": "EURUSD", "timeframe": "15m"}, None)})
    out = await t.watch_this_chart()
    assert out["status"] == "DONE"
    assert "watch" in out["summary"].lower()
    assert rt.requests[0].skill == "tradingview" and rt.requests[0].action == "status"


async def test_watch_this_chart_reports_not_a_chart_when_nothing_to_watch():
    t, rt, _ = tools({("tradingview", "status"): (ActionStatus.FAILED, {}, "tradingview window not found")})
    out = await t.watch_this_chart()
    assert out["status"] == "NOT_A_CHART"


async def test_switching_to_mt5_after_tradingview_changes_current_trading_app():
    t, rt, _ = tools()
    await t.desktop_open_app("tradingview")
    assert t.current_trading_app == "TradingView"
    await t.desktop_open_app("mt5")
    assert t.current_trading_app == "MetaTrader 5"


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
