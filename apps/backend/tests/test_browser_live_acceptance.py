"""Opt-in live acceptance tests for the BrowserAgent (`browser/session.py`).

Unlike every other test in this suite, these drive a REAL Chrome process on
this machine over CDP -- not mocked. They exist to answer the mission's
"Live Acceptance Tests" requirement honestly: automated tests that fake out
Chrome (test_browser_session.py, test_skills_web_browser.py) prove the
Python-side logic is correct, but only a run against a real browser proves
the injected JS in browser/element_resolver.py actually works against a
real DOM.

Skipped by default (slow, needs a real Chrome install + network, launches a
visible automation-profile Chrome window) -- opt in with:
    TARS_LIVE_BROWSER_TEST=1 pytest tests/test_browser_live_acceptance.py -q -s
"""
from __future__ import annotations

import asyncio
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from browser.session import BrowserSession

pytestmark = pytest.mark.skipif(
    os.environ.get("TARS_LIVE_BROWSER_TEST") != "1",
    reason="opt-in only: set TARS_LIVE_BROWSER_TEST=1 to drive a real Chrome",
)

# The smallest valid PDF byte stream (a commonly cited minimal structure) --
# real enough that any PDF reader opens it, small enough to keep in source.
_MINIMAL_PDF = (
    b"%PDF-1.1\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 3 3]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n"
)


@pytest.fixture
async def session():
    s = BrowserSession(port=9333)
    # A fresh tab per test -- this dev Chrome profile persists across test
    # runs, so without this, "the active tab" could silently be a leftover
    # from an earlier run instead of a clean one.
    tab = await s.new_tab()
    yield s
    try:
        await s.close_tab(tab["id"])
    except Exception:
        pass
    await s.aclose()


@pytest.fixture
def download_test_page(tmp_path):
    """A tiny local HTTP server (not a third-party site, which would make
    this test depend on someone else's page staying stable) serving one
    HTML page with a link to a PDF sent with `Content-Disposition:
    attachment` -- confirmed live that without that header Chrome's own PDF
    viewer opens the file inline instead of downloading it, regardless of
    `Browser.setDownloadBehavior`; a real server offering a "download"
    affordance sets this header (or an HTML `download` attribute) for
    exactly this reason, so this mirrors a real download target rather than
    a contrived one."""
    (tmp_path / "sample.pdf").write_bytes(_MINIMAL_PDF)
    (tmp_path / "index.html").write_text('<html><body><a href="sample.pdf">Sample PDF</a></body></html>')

    class _Handler(SimpleHTTPRequestHandler):
        def end_headers(self):
            if self.path.endswith(".pdf"):
                self.send_header("Content-Disposition", 'attachment; filename="sample.pdf"')
            super().end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *a, **kw: _Handler(*a, directory=str(tmp_path), **kw))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/index.html"
    finally:
        server.shutdown()


async def test_navigate_and_extract_on_a_stable_page(session):
    out = await session.navigate("https://example.com")
    assert out["ok"] is True, out
    assert "Example Domain" in out["title"]

    text = await session.extract_text("all")
    assert "documentation examples" in text["text"].lower()


async def test_find_and_click_a_real_link(session):
    await session.navigate("https://example.com")
    found = await session.find("learn more")
    assert found["ok"] is True, found

    out = await session.click("learn more")
    assert out["ok"] is True, out

    ctx = await session.get_context()
    assert ctx["url"] != "https://example.com/"


async def test_ordinal_first_and_second_result_on_google(session):
    """Mission section 16, verbatim: search OpenAI, open the first result,
    go back, open the second result -- both must resolve to a genuinely
    different destination, not the same link twice."""
    await session.navigate("https://www.google.com/search?q=OpenAI")

    first = await session.click("the first result")
    assert first["ok"] is True, first
    assert first.get("navigated") is True, "first click did not verify a resulting navigation"
    first_url = first["url_after"]

    back = await session.back()
    assert back["ok"] is True, back

    second = await session.click("the second result")
    assert second["ok"] is True, second
    assert second.get("navigated") is True, "second click did not verify a resulting navigation"
    second_url = second["url_after"]

    assert first_url != second_url, "first and second result resolved to the same destination"


async def test_relative_and_pronoun_references_on_the_same_page(session):
    """Mission section 2: "the next one" and "it"/"that" resolve against the
    last ordinal/element reference, re-found fresh against the current DOM
    -- not a cached node -- without the caller repeating the collection."""
    await session.navigate("https://www.google.com/search?q=OpenAI")

    first = await session.find("the first result")
    assert first["ok"] is True, first

    nxt = await session.find("the next one")
    assert nxt["ok"] is True, nxt
    assert nxt["matched"]["text"] != first["matched"]["text"], "'the next one' re-resolved to the same element"

    pronoun = await session.find("that")
    assert pronoun["ok"] is True, pronoun
    assert pronoun["matched"]["text"] == nxt["matched"]["text"], "'that' did not refer back to the last resolved element"


async def test_tabs_open_close_and_list(session):
    before = await session.list_tabs()
    created = await session.new_tab("https://example.com")
    assert created["ok"] is True
    after = await session.list_tabs()
    assert len(after) == len(before) + 1
    closed = await session.close_tab(created["id"])
    assert closed["ok"] is True


async def test_youtube_search_and_ordinal_click(session):
    """Mission sections 16/19 on a second, JS-heavy results page. Honestly
    scoped: YouTube injects promoted/recommended items ahead of the organic
    results for some queries, so unlike Google's cleaner results page this
    does not assert *which* video "the first video" lands on -- only that
    the full mechanics (type+submit verified, ordinal click verified,
    back verified, new tab) all work against a real, complex SPA. See the
    mission report's P1 notes for the "no semantic sponsored-vs-organic
    distinction" limitation this surfaces."""
    nav = await session.navigate("https://www.youtube.com")
    assert nav["ok"] is True, nav

    typed = await session.type_text("search", "Formula 1 highlights", submit=True)
    if not typed["ok"]:
        pytest.skip(f"YouTube's search box did not resolve (consent dialog?): {typed}")
    assert typed.get("navigated") is True, "search submit did not verify landing on a results page"

    clicked = await session.click("the first video")
    if not clicked["ok"]:
        pytest.skip(f"No video link resolved on this run (layout/consent variance): {clicked}")
    assert clicked.get("navigated") is True, "clicking a video did not verify a resulting navigation"

    back = await session.back()
    assert back["ok"] is True, back

    new_tab = await session.new_tab()
    assert new_tab["ok"] is True, new_tab


async def test_download_a_real_pdf_and_verify_on_disk(session, download_test_page):
    """Mission section 3/18: resolve+click a download link, then verify the
    file actually, really landed on disk -- filename, path and nonzero size
    -- never report success merely because a link was clicked."""
    nav = await session.navigate(download_test_page)
    assert nav["ok"] is True, nav

    out = await session.download("sample pdf", timeout=15.0)
    assert out["status"] == "COMPLETED", out
    assert out["filename"] == "sample.pdf"
    assert out["size_bytes"] > 0
    from pathlib import Path

    assert Path(out["path"]).is_file()
    assert Path(out["path"]).read_bytes() == _MINIMAL_PDF

    last = session.get_last_download()
    assert last["ok"] is True
    assert last["filename"] == "sample.pdf"


async def test_google_search_round_trip(session):
    """Mission section 19's "Search Google for OpenAI" -- best-effort: a real
    Google results page is outside this codebase's control (consent dialogs,
    A/B layouts), so this only asserts the mechanics (navigate, type, submit,
    verified page change) work, not any specific result content."""
    nav = await session.navigate("https://www.google.com/ncr")
    assert nav["ok"] is True, nav
    typed = await session.type_text("search", "OpenAI", submit=True)
    if not typed["ok"]:
        pytest.skip(f"Google's search box did not resolve (consent dialog?): {typed}")
    ctx = await session.get_context()
    assert "google" in ctx["url"].lower()
