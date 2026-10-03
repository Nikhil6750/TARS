"""Unit tests for PrimaryVisibleUserWindow (skills/_desktop_automation.py).

Live-testing found TARS answering "what is on my screen" with a stale,
previously-opened app (or its own UI) instead of the real foreground
content -- see the module docstring additions in _desktop_automation.py.
These tests fake the Win32 window-enumeration layer so the exclusion logic
(TARS's own window, invisible/minimized/tool windows, invalid bounds) can be
verified deterministically without depending on real OS window state.
"""
from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="win32gui is Windows-only")

import win32con  # noqa: E402
import win32gui  # noqa: E402
import win32process  # noqa: E402

import skills._desktop_automation as da  # noqa: E402
from app.action_contracts import WindowBounds  # noqa: E402


class _FakeWindow:
    def __init__(self, *, title, exe, pid=100, visible=True, iconic=False, tool=False,
                 bounds=(0, 0, 800, 600)):
        self.title = title
        self.exe = exe
        self.pid = pid
        self.visible = visible
        self.iconic = iconic
        self.tool = tool
        self.bounds = bounds


@pytest.fixture
def fake_desktop(monkeypatch):
    """Registers a fake Z-ordered window list (first = topmost) and patches
    every win32 call _desktop_automation.py's PrimaryVisibleUserWindow path
    uses, so tests can assert on exclusion behavior without a real desktop."""
    windows: dict[int, _FakeWindow] = {}

    def set_windows(ordered: list[_FakeWindow]) -> None:
        windows.clear()
        for i, w in enumerate(ordered, start=1):
            windows[i] = w

    def fake_enum_windows(callback, extra):
        for hwnd in list(windows.keys()):
            callback(hwnd, extra)

    def fake_is_window_visible(hwnd):
        return windows[hwnd].visible

    def fake_is_iconic(hwnd):
        return windows[hwnd].iconic

    def fake_get_window_text(hwnd):
        return windows[hwnd].title

    def fake_get_window_thread_process_id(hwnd):
        return (0, windows[hwnd].pid)

    def fake_get_window_long(hwnd, index):
        if index == win32con.GWL_EXSTYLE and windows[hwnd].tool:
            return win32con.WS_EX_TOOLWINDOW
        return 0

    def fake_process_executable_name(pid):
        for w in windows.values():
            if w.pid == pid:
                return w.exe
        return ""

    def fake_capture_window_bounds(hwnd):
        x, y, right, bottom = windows[hwnd].bounds
        width, height = right - x, bottom - y
        if width <= 0 or height <= 0:
            return None
        return WindowBounds(x=x, y=y, width=width, height=height)

    monkeypatch.setattr(win32gui, "EnumWindows", fake_enum_windows)
    monkeypatch.setattr(win32gui, "IsWindowVisible", fake_is_window_visible)
    monkeypatch.setattr(win32gui, "IsIconic", fake_is_iconic)
    # win32gui.IsZoomed is absent on some pywin32 builds -- capture_window_state's
    # _safe() wrapper already degrades that AttributeError to False, so it is
    # deliberately not patched here.
    monkeypatch.setattr(win32gui, "IsWindow", lambda hwnd: True)
    monkeypatch.setattr(win32gui, "GetWindowText", fake_get_window_text)
    monkeypatch.setattr(win32gui, "GetWindowLong", fake_get_window_long)
    monkeypatch.setattr(win32process, "GetWindowThreadProcessId", fake_get_window_thread_process_id)
    monkeypatch.setattr(da, "_process_executable_name", fake_process_executable_name)
    monkeypatch.setattr(da, "capture_window_bounds", fake_capture_window_bounds)
    # Real monitor/focused-control lookups don't make sense against fake
    # hwnds -- not what these tests are verifying, so isolate them rather
    # than risk a real (uncaught, non-OSError) pywintypes.error.
    monkeypatch.setattr(da, "capture_monitor", lambda hwnd: None)
    monkeypatch.setattr(da, "capture_focused_control_info", lambda: None)

    return set_windows


def test_tars_window_matches_known_title_variants():
    for title in ("TARS", "TARS Ready", "tars companion", "  TARS  "):
        assert da._is_tars_window("", title) is True
    for title in ("TARS Volume Mixer", "My TARS Notes - Notepad", "Chrome"):
        assert da._is_tars_window("", title) is False


def test_primary_user_window_skips_tars_overlay_on_top(fake_desktop):
    fake_desktop([
        _FakeWindow(title="TARS", exe="tars.exe", pid=1),
        _FakeWindow(title="Chrome - YouTube Music", exe="chrome.exe", pid=2),
    ])
    primary = da.get_primary_visible_user_window()
    assert primary is not None
    assert primary[2] == "Chrome - YouTube Music"


def test_primary_user_window_skips_minimized_and_invisible_and_tool_windows(fake_desktop):
    fake_desktop([
        _FakeWindow(title="Hidden App", exe="hidden.exe", pid=1, visible=False),
        _FakeWindow(title="Minimized App", exe="min.exe", pid=2, iconic=True),
        _FakeWindow(title="Floating Toolbar", exe="tool.exe", pid=3, tool=True),
        _FakeWindow(title="", exe="notitle.exe", pid=4),
        _FakeWindow(title="Zero Bounds App", exe="zero.exe", pid=5, bounds=(0, 0, 0, 0)),
        _FakeWindow(title="TradingView", exe="tradingview.exe", pid=6),
    ])
    primary = da.get_primary_visible_user_window()
    assert primary is not None
    assert primary[2] == "TradingView"


def test_resolve_window_current_prefers_primary_user_window_over_raw_foreground(fake_desktop, monkeypatch):
    fake_desktop([
        _FakeWindow(title="TARS", exe="tars.exe", pid=1),
        _FakeWindow(title="Notepad", exe="notepad.exe", pid=2),
    ])
    # Raw GetForegroundWindow() reports TARS itself -- resolve_window(None)
    # must still return the real content window, not TARS and not a
    # remembered previous app.
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 1)
    hwnd, exe, title = da.resolve_window(None)
    assert (exe, title) == ("tars.exe", "Notepad") or title == "Notepad"
    assert title == "Notepad"


def test_resolve_window_falls_back_to_raw_foreground_when_nothing_else_is_eligible(fake_desktop, monkeypatch):
    fake_desktop([
        _FakeWindow(title="TARS", exe="tars.exe", pid=1),
    ])
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 1)
    hwnd, exe, title = da.resolve_window("current")
    assert title == "TARS"


def test_visible_screen_state_reports_overlay_separately_from_primary(fake_desktop, monkeypatch):
    fake_desktop([
        _FakeWindow(title="TARS", exe="tars.exe", pid=1),
        _FakeWindow(title="TradingView", exe="tradingview.exe", pid=2),
    ])
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 1)
    state = da.get_visible_screen_state()
    assert state.raw_foreground_window is not None
    assert state.raw_foreground_window.window_title == "TARS"
    assert state.primary_user_window is not None
    assert state.primary_user_window.window_title == "TradingView"
    assert len(state.tars_overlay_windows) == 1
    assert state.tars_overlay_windows[0].window_title == "TARS"
