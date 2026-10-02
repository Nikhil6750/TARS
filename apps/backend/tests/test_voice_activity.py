"""voice/activity.py's deterministic tool-call/tool-result text formatter --
pure functions, no browser/voice session needed."""
from __future__ import annotations

from voice.activity import describe_tool_call, describe_tool_result


def test_open_app():
    assert describe_tool_call("desktop_open_app", {"target": "Chrome"}) == "Opening Chrome…"


def test_focus_window():
    assert describe_tool_call("desktop_focus_window", {"target": "TradingView"}) == "Switching to TradingView…"


def test_navigate_known_site():
    assert describe_tool_call("web_navigate", {"url": "https://www.youtube.com/results?x=1"}) == "Opening YouTube…"


def test_navigate_unknown_site():
    assert describe_tool_call("web_navigate", {"url": "https://example.com/page"}) == "Opening Example…"


def test_search_via_type_with_submit():
    out = describe_tool_call("web_type", {"target": "search box", "text": "Formula 1 highlights", "submit": True})
    assert out == "Searching for Formula 1 highlights…"


def test_type_without_submit():
    out = describe_tool_call("web_type", {"target": "the comment box", "text": "hello"})
    assert out == "Typing 'hello' into the comment box…"


def test_click_first_result():
    assert describe_tool_call("web_click", {"target": "the first result"}) == "Opening the first result…"


def test_click_video():
    assert describe_tool_call("web_click", {"target": "the first video"}) == "Playing a video…"


def test_click_generic():
    assert describe_tool_call("web_click", {"target": "sign in"}) == "Clicking sign in…"


def test_download_with_target():
    assert describe_tool_call("web_download", {"target": "report.pdf"}) == "Downloading report.pdf…"


def test_files_open_uses_basename():
    assert describe_tool_call("files_read_open", {"path": "C:\\Users\\me\\Downloads"}) == "Opening Downloads…"


def test_desktop_type_into_control_uses_label():
    assert describe_tool_call("desktop_type_text", {"control_id": "x1", "label": "Notepad"}) == "Typing into Notepad…"


def test_terminal_and_claude():
    assert describe_tool_call("run_terminal", {"command": "dir"}) == "Running a command…"
    assert describe_tool_call("ask_claude", {"question": "why"}) == "Thinking it through…"


def test_unmapped_tool_falls_back_generically():
    out = describe_tool_call("some_future_tool", {})
    assert out == "Some future tool…"


def test_result_done():
    assert describe_tool_result("desktop_open_app", {"target": "Calculator"}, "DONE") == "Done"


def test_result_not_found_uses_real_target():
    assert describe_tool_result("desktop_open_app", {"target": "Calculator"}, "NOT_FOUND") == "Couldn't find Calculator"


def test_result_ambiguous():
    out = describe_tool_result("web_click", {"target": "buy now"}, "AMBIGUOUS")
    assert out == "'buy now' was ambiguous"


def test_result_blocked():
    assert describe_tool_result("run_terminal", {"command": "rm -rf /"}, "BLOCKED") == "That's not allowed"
