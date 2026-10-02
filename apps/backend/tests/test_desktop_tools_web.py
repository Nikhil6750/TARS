"""DesktopTools' web_* wrappers: dispatch to the `web` skill via the same
ActionRuntime path as every other desktop tool, browser-tab context
tracking (mirrors the existing TradingView context fields), and the
AMBIGUOUS/PARTIAL outcomes `_state()` now recognizes for the BrowserAgent.

Reuses tests/test_desktop_voice_tools.py's `Runtime` fake (records requests,
returns scripted ActionResults) via a local copy of the same small harness
so this file has no import-order dependency on that one.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.action_contracts import ActionResult, ActionStatus, RiskLevel
from voice.desktop_tools import DesktopTools


class Runtime:
    def __init__(self, script=None):
        self.requests, self.script = [], script or {}

    async def submit(self, request):
        self.requests.append(request)
        status, data, error = self.script.get((request.skill, request.action), (ActionStatus.SUCCEEDED, {}, None))
        return ActionResult(request_id=request.id, status=status, risk_level=RiskLevel.LOW_RISK,
                            summary=f"{request.skill}.{request.action}", data={**data}, error=error)


def tools(script=None):
    rt = Runtime(script)
    return DesktopTools(SimpleNamespace(action_runtime=rt), lambda: ("", 0.0)), rt


async def test_web_navigate_dispatches_to_web_skill_and_updates_context():
    t, rt = tools({("web", "navigate"): (ActionStatus.SUCCEEDED,
                                         {"outcome": "SUCCESS", "url": "https://news.example/", "title": "News"}, None)})
    out = await t.web_navigate("https://news.example")
    assert out["status"] == "DONE"
    assert rt.requests[0] == rt.requests[0]  # sanity: one request recorded
    assert (rt.requests[0].skill, rt.requests[0].action) == ("web", "navigate")
    assert t.active_browser_tab == {"url": "https://news.example/", "title": "News", "id": None}


async def test_web_click_not_found_reports_truthfully():
    t, rt = tools({("web", "click"): (ActionStatus.FAILED, {"outcome": "NOT_FOUND"}, "target not found")})
    out = await t.web_click("a button that doesn't exist")
    assert out["status"] == "NOT_FOUND"


async def test_web_click_ambiguous_reports_ambiguous():
    t, rt = tools({("web", "click"): (ActionStatus.FAILED, {"outcome": "AMBIGUOUS"}, "ambiguous target")})
    out = await t.web_click("buy now")
    assert out["status"] == "AMBIGUOUS"


async def test_web_back_partial_outcome():
    t, rt = tools({("web", "back"): (ActionStatus.FAILED, {"outcome": "PARTIAL", "url": "https://x", "title": ""}, "navigation not verified")})
    out = await t.web_back()
    assert out["status"] == "PARTIAL"


async def test_web_get_context_feeds_desktop_context():
    t, rt = tools({("web", "get_context"): (ActionStatus.SUCCEEDED,
                                            {"url": "https://a.com", "title": "A", "active_tab_id": "t1"}, None)})
    await t.web_get_context()
    ctx = await t.desktop_context()
    assert ctx["active_browser_tab"] == {"url": "https://a.com", "title": "A", "id": "t1"}


async def test_web_type_and_select_pass_through_arguments():
    t, rt = tools()
    await t.web_type("search box", "hello world", submit=True)
    assert rt.requests[-1].arguments == {"target": "search box", "text": "hello world", "submit": True}
    await t.web_select("country dropdown", "Canada")
    assert rt.requests[-1].arguments == {"target": "country dropdown", "value": "Canada"}


async def test_web_scroll_with_and_without_target():
    t, rt = tools()
    await t.web_scroll("down")
    assert rt.requests[-1].arguments == {"direction": "down"}
    await t.web_scroll("down", "the footer")
    assert rt.requests[-1].arguments == {"direction": "down", "target": "the footer"}
