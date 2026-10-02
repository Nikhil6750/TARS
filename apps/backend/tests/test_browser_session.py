"""BrowserSession tests -- CDP/Chrome are faked out entirely (no real browser
needed) so these run in CI; `test_browser_live_acceptance.py` is the
opt-in-only suite that drives a real Chrome.

The fakes sit at the same seam `BrowserSession` itself defines: an
`http` object shaped like `browser.cdp.CDPBrowser` and element resolution
results shaped like what a real page's JS would return (see
`browser/element_resolver.py`'s module docstring for that contract) --
`navigate`/`back`/`forward`/`find`/`click`/etc. are patched at the
`browser.cdp.evaluate`/`CDPTarget` boundary, not reimplemented.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from browser.cdp import CDPError
from browser.session import BrowserLaunchError, BrowserSession


class _FakeHTTP:
    """Fakes browser/cdp.py's CDPBrowser HTTP surface against an in-memory
    list of targets, so BrowserSession's tab bookkeeping is exercised for
    real without any network/process involved."""

    def __init__(self, targets: list[dict] | None = None, *, alive: bool = True):
        self.targets = targets if targets is not None else [
            {"id": "t1", "type": "page", "url": "https://example.com", "title": "Example",
             "webSocketDebuggerUrl": "ws://fake/t1"},
        ]
        self.alive = alive
        self.activated: list[str] = []
        self.closed: list[str] = []

    async def version(self, *, timeout: float = 2.0):
        return {"Browser": "fake"} if self.alive else None

    async def aclose(self):
        pass

    async def list_targets(self, *, timeout: float = 10.0):
        return list(self.targets)

    async def new_tab(self, url: str, *, timeout: float = 10.0):
        tab = {"id": f"t{len(self.targets) + 1}", "type": "page", "url": url, "title": url,
               "webSocketDebuggerUrl": f"ws://fake/t{len(self.targets) + 1}"}
        self.targets.append(tab)
        return tab

    async def close_tab(self, target_id: str, *, timeout: float = 10.0):
        self.closed.append(target_id)
        self.targets = [t for t in self.targets if t["id"] != target_id]

    async def activate_tab(self, target_id: str, *, timeout: float = 10.0):
        self.activated.append(target_id)


def _session(http: _FakeHTTP | None = None) -> BrowserSession:
    return BrowserSession(http=http or _FakeHTTP())


# ---- tab management -----------------------------------------------------

async def test_get_context_reports_active_tab():
    session = _session()
    ctx = await session.get_context()
    assert ctx["url"] == "https://example.com"
    assert ctx["tab_count"] == 1
    assert ctx["active_tab_index"] == 0


async def test_list_tabs_marks_active():
    http = _FakeHTTP(targets=[
        {"id": "t1", "type": "page", "url": "https://a.com", "title": "A", "webSocketDebuggerUrl": "ws://a"},
        {"id": "t2", "type": "page", "url": "https://b.com", "title": "B", "webSocketDebuggerUrl": "ws://b"},
    ])
    session = _session(http)
    tabs = await session.list_tabs()
    assert len(tabs) == 2
    assert tabs[0]["active"] is True  # defaults to the first page target
    assert tabs[1]["active"] is False


async def test_focus_tab_by_index_and_title_substring():
    http = _FakeHTTP(targets=[
        {"id": "t1", "type": "page", "url": "https://a.com", "title": "Alpha", "webSocketDebuggerUrl": "ws://a"},
        {"id": "t2", "type": "page", "url": "https://b.com", "title": "Bravo", "webSocketDebuggerUrl": "ws://b"},
    ])
    session = _session(http)
    out = await session.focus_tab("1")
    assert out["ok"] is True and out["id"] == "t2"
    out2 = await session.focus_tab("alpha")
    assert out2["ok"] is True and out2["id"] == "t1"
    assert http.activated == ["t2", "t1"]


async def test_focus_tab_not_found():
    session = _session()
    out = await session.focus_tab("nonexistent")
    assert out == {"ok": False, "reason": "NOT_FOUND", "query": "nonexistent"}


async def test_new_tab_becomes_active_and_validates_scheme():
    session = _session()
    out = await session.new_tab("https://news.example")
    assert out["ok"] is True
    assert session._active_target_id == out["id"]
    with pytest.raises(Exception):
        await session.new_tab("javascript:alert(1)")


async def test_close_tab_defaults_to_active():
    http = _FakeHTTP()
    session = _session(http)
    await session.get_context()  # establishes active target
    out = await session.close_tab("")
    assert out["ok"] is True
    assert http.closed == ["t1"]
    assert session._active_target_id is None


# ---- launch behavior ------------------------------------------------------

async def test_ensure_started_reuses_already_running_browser():
    http = _FakeHTTP(alive=True)
    session = _session(http)
    with patch("browser.session.subprocess.Popen") as popen:
        await session.ensure_started()
        popen.assert_not_called()


async def test_ensure_started_raises_when_no_chrome_installed():
    http = _FakeHTTP(alive=False)
    fake_resolver = type("R", (), {"resolve": lambda self, q: type(
        "Result", (), {"outcome": "NOT_FOUND", "app": None})()})()
    session = BrowserSession(http=http, resolver=fake_resolver)
    with pytest.raises(BrowserLaunchError):
        await session.ensure_started()


# ---- navigation verification ----------------------------------------------

async def test_navigate_reports_success_when_page_becomes_ready():
    session = _session()
    with patch("browser.session.CDPTarget") as target_cls, \
         patch("browser.session.evaluate", new_callable=AsyncMock) as fake_eval:
        target_cls.return_value.send = AsyncMock(return_value={})
        fake_eval.side_effect = ["complete", "https://news.example/", "News"]
        out = await session.navigate("https://news.example")
    assert out["ok"] is True
    assert out["url"] == "https://news.example/"


async def test_navigate_reports_partial_when_same_host_but_not_ready():
    session = _session()
    with patch("browser.session.CDPTarget") as target_cls, \
         patch("browser.session.evaluate", new_callable=AsyncMock) as fake_eval, \
         patch("browser.session._POLL_INTERVAL", 0), \
         patch("browser.session._NAV_TIMEOUT", 0):
        target_cls.return_value.send = AsyncMock(return_value={})
        fake_eval.side_effect = ["https://news.example/slow", "News (loading)"]
        out = await session.navigate("https://news.example")
    assert out["ok"] is False
    assert out["url"] == "https://news.example/slow"


async def test_navigate_rejects_non_http_scheme():
    session = _session()
    with pytest.raises(Exception):
        await session.navigate("file:///etc/passwd")


# ---- semantic element resolution -------------------------------------------

async def test_click_success_reports_matched_element():
    session = _session()
    with patch("browser.session.evaluate", new_callable=AsyncMock) as fake_eval:
        fake_eval.return_value = {"ok": True, "matched": {"tag": "button", "text": "Sign in"}}
        out = await session.click("sign in")
    assert out["ok"] is True
    assert out["matched"]["text"] == "Sign in"


async def test_click_not_found():
    session = _session()
    with patch("browser.session.evaluate", new_callable=AsyncMock) as fake_eval:
        fake_eval.return_value = {"ok": False, "reason": "NOT_FOUND", "candidates": []}
        out = await session.click("a button that does not exist")
    assert out["ok"] is False
    assert out["reason"] == "NOT_FOUND"


async def test_click_ambiguous_returns_candidates():
    session = _session()
    with patch("browser.session.evaluate", new_callable=AsyncMock) as fake_eval:
        fake_eval.return_value = {
            "ok": False, "reason": "AMBIGUOUS",
            "candidates": [{"text": "Buy now (A)"}, {"text": "Buy now (B)"}],
        }
        out = await session.click("buy now")
    assert out["reason"] == "AMBIGUOUS"
    assert len(out["candidates"]) == 2


async def test_wait_for_polls_until_found():
    session = _session()
    calls = {"n": 0}

    async def fake_find(target):
        calls["n"] += 1
        if calls["n"] < 3:
            return {"ok": False, "reason": "NOT_FOUND"}
        return {"ok": True, "matched": {"text": target}}

    with patch.object(session, "find", side_effect=fake_find), \
         patch("browser.session._POLL_INTERVAL", 0):
        out = await session.wait_for("results", timeout=1.0)
    assert out["ok"] is True
    assert calls["n"] == 3


async def test_wait_for_times_out_honestly():
    session = _session()
    with patch.object(session, "find", new_callable=AsyncMock) as fake_find, \
         patch("browser.session._POLL_INTERVAL", 0):
        fake_find.return_value = {"ok": False, "reason": "NOT_FOUND"}
        out = await session.wait_for("ghost element", timeout=0.05)
    assert out["ok"] is False


# ---- extraction -------------------------------------------------------------

async def test_extract_text_summary_mode():
    session = _session()
    with patch("browser.session.evaluate", new_callable=AsyncMock) as fake_eval:
        fake_eval.return_value = "line one\nline two"
        out = await session.extract_text("summary")
    assert out["ok"] is True
    assert out["text"] == "line one\nline two"


async def test_get_links_returns_list():
    session = _session()
    with patch("browser.session.evaluate", new_callable=AsyncMock) as fake_eval:
        fake_eval.return_value = [{"text": "Docs", "href": "https://docs.example"}]
        out = await session.get_links()
    assert out["ok"] is True
    assert out["links"][0]["href"] == "https://docs.example"


async def test_aclose_closes_all_cached_connections():
    session = _session()
    with patch("browser.session.evaluate", new_callable=AsyncMock) as fake_eval, \
         patch("browser.session.CDPTarget") as target_cls:
        fake_eval.return_value = {"ok": True, "matched": {"text": "x"}}
        conn = target_cls.return_value
        conn.close = AsyncMock()
        await session.find("x")
        assert session._connections  # one cached connection after a real call
        await session.aclose()
        conn.close.assert_awaited_once()
        assert session._connections == {}


async def test_cdp_error_propagates_from_evaluate():
    session = _session()
    with patch("browser.session.evaluate", new_callable=AsyncMock, side_effect=CDPError("boom")):
        with pytest.raises(CDPError):
            await session.find("anything")
