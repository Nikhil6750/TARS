"""Windows UI Automation + win32 helpers backing `skills/desktop_control.py`.

Split out of desktop_control.py because it is meaningfully larger than the
other Wave 2A skills (real UIA COM interop, not just win32gui). Everything
here is either read-only introspection or a single, explicit state-changing
UIA pattern call -- there is no keystroke/coordinate-click simulation and no
fallback to blind screen coordinates when a semantic UIA target exists, per
the Wave 2B spec.

Control identity: raw UIA elements are not JSON-serializable and are only
valid while the underlying COM element is alive, so `list_controls` /
`inspect_current_window` hand back an opaque `control_id` (a random token,
not a pointer or runtime-id encoding) that resolves through `CONTROL_CACHE`,
a small bounded/TTL'd in-memory map -- not a "raw UI tree" retained
indefinitely, just enough state to let a follow-up focus/invoke/type/select/
scroll action target the same element it was just shown.
"""
from __future__ import annotations

import re
import time
import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

import comtypes
import uiautomation as auto
import win32api
import win32clipboard
import win32con
import win32gui
import win32process

from app.action_contracts import (
    ActiveWindowContext,
    ContextSource,
    FocusedControlInfo,
    MonitorInfo,
    SkillExecutionError,
    VisibleScreenState,
    WindowBounds,
    WindowState,
)
from skills.windows_app import _find_window, _process_executable_name

# TARS's own companion/orb/activity-pill window (apps/web/src-tauri,
# tauri.conf.json productName "TARS") is a *different OS process* from this
# backend, so it cannot be excluded by PID like `get_primary_visible_user_window`
# excludes utility windows of unrelated processes -- title is the only signal
# available. Matches "TARS", "TARS Ready" (set once the frontend finishes
# loading, see lib.rs's mark_frontend_ready) and "TARS Companion"/"TARS ..."
# variants; deliberately anchored so it does not also exclude an unrelated
# window that merely mentions TARS in its title (e.g. a browser tab).
_TARS_WINDOW_TITLE_RE = re.compile(r"^\s*tars(\s+(ready|companion)\b)?\s*$", re.IGNORECASE)
_TARS_EXE_RE = re.compile(r"^tars(-companion)?\.exe$", re.IGNORECASE)
# Windows that are real top-level windows but never meaningful "user
# content" for a "what's on my screen" answer -- the desktop shell itself,
# and IME/input-experience helper windows some Windows builds surface as
# visible, titled top-level windows.
_UTILITY_TITLE_RE = re.compile(
    r"^(program manager|windows input experience|microsoft text input application)$",
    re.IGNORECASE,
)

ACTIONABLE_CONTROL_TYPES = {
    "ButtonControl",
    "CheckBoxControl",
    "RadioButtonControl",
    "ComboBoxControl",
    "EditControl",
    "ListControl",
    "ListItemControl",
    "MenuControl",
    "MenuItemControl",
    "MenuBarControl",
    "TabControl",
    "TabItemControl",
    "HyperlinkControl",
    "SliderControl",
    "TreeItemControl",
    "DataItemControl",
    "DocumentControl",
    "SpinnerControl",
    "SplitButtonControl",
}

_DEFAULT_MAX_CONTROLS = 100
_HARD_MAX_CONTROLS = 300
_DEFAULT_MAX_DEPTH = 6
_MAX_TEXT_CHARS = 10_000


def _safe(getter: Any, default: Any = None) -> Any:
    """Best-effort UIA property read. Elements can go stale mid-call (the
    app repaints, a menu closes) -- that is a normal race, not a bug, so it
    degrades to `default` rather than raising."""
    try:
        return getter()
    except (comtypes.COMError, OSError, AttributeError):
        return default


def _raw_foreground_window() -> tuple[int, str, str] | None:
    """The literal OS foreground window, no exclusions applied. Kept
    separate from `get_primary_visible_user_window` because this value can
    legitimately BE TARS's own window (e.g. the user just clicked into the
    TARS panel) -- that is a true fact worth reporting as
    `raw_foreground_window`, just never the answer to "what's on my
    screen"."""
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return None
    title = win32gui.GetWindowText(hwnd)
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    exe = _process_executable_name(pid) if pid else ""
    return hwnd, exe, title


def _is_tars_window(exe: str, title: str) -> bool:
    return bool(_TARS_EXE_RE.match((exe or "").strip())) or bool(
        _TARS_WINDOW_TITLE_RE.match((title or "").strip())
    )


def _window_is_tool_window(hwnd: int) -> bool:
    ex_style = _safe(lambda: win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE), 0)
    return bool(ex_style & win32con.WS_EX_TOOLWINDOW)


def _window_has_valid_bounds(hwnd: int) -> bool:
    bounds = capture_window_bounds(hwnd)
    return bounds is not None and bounds.width > 0 and bounds.height > 0


def _enum_top_level_windows_z_order() -> list[int]:
    """EnumWindows enumerates top-level windows in Z-order, topmost window
    first -- exactly the ordering PrimaryVisibleUserWindow needs to walk."""
    hwnds: list[int] = []

    def _callback(hwnd: int, _extra: None) -> bool:
        hwnds.append(hwnd)
        return True

    try:
        win32gui.EnumWindows(_callback, None)
    except Exception:
        pass
    return hwnds


def _window_snapshot(hwnd: int) -> tuple[int, str, str] | None:
    try:
        title = win32gui.GetWindowText(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        exe = _process_executable_name(pid) if pid else ""
    except Exception:
        return None
    return hwnd, exe, title


def get_primary_visible_user_window() -> tuple[int, str, str] | None:
    """The first real, visible, user-content window in Z-order (topmost
    first), excluding:
      - TARS's own companion/orb/activity-pill window(s)
      - invisible or minimized windows
      - tool windows (WS_EX_TOOLWINDOW) and known desktop-shell/IME utility
        windows that are not meaningful user content
      - windows with no title or zero/invalid bounds

    This is the truthful answer to "what is on my screen" / deictic
    requests ("this", "here", "this chart") -- it always wins over
    GetForegroundWindow() alone (which can be TARS itself) and over any
    remembered/previous-turn app name."""
    for hwnd in _enum_top_level_windows_z_order():
        if not _safe(lambda h=hwnd: win32gui.IsWindowVisible(h), False):
            continue
        if _safe(lambda h=hwnd: win32gui.IsIconic(h), False):
            continue
        snapshot = _window_snapshot(hwnd)
        if snapshot is None:
            continue
        _, exe, title = snapshot
        if not title.strip():
            continue
        if _is_tars_window(exe, title):
            continue
        if _UTILITY_TITLE_RE.match(title.strip()):
            continue
        if _window_is_tool_window(hwnd):
            continue
        if not _window_has_valid_bounds(hwnd):
            continue
        return snapshot
    return None


def get_tars_overlay_windows() -> list[tuple[int, str, str]]:
    """Every visible top-level window identified as TARS's own UI
    (companion/orb/activity-pill/workspace) -- reported alongside
    `primary_user_window` so a caller can note "TARS is also visible"
    without ever letting it replace the real answer."""
    overlays: list[tuple[int, str, str]] = []
    for hwnd in _enum_top_level_windows_z_order():
        if not _safe(lambda h=hwnd: win32gui.IsWindowVisible(h), False):
            continue
        snapshot = _window_snapshot(hwnd)
        if snapshot is None:
            continue
        _, exe, title = snapshot
        if _is_tars_window(exe, title):
            overlays.append(snapshot)
    return overlays


def get_visible_screen_state() -> VisibleScreenState:
    """Full Z-order-aware snapshot: the literal foreground window, the
    primary visible *user* window (TARS and non-content windows excluded),
    and any of TARS's own overlay windows currently visible. See
    VisibleScreenState's docstring for why these are kept separate."""
    raw = _raw_foreground_window()
    primary = get_primary_visible_user_window()
    overlays = get_tars_overlay_windows()
    return VisibleScreenState(
        raw_foreground_window=_context_for(*raw) if raw else None,
        primary_user_window=_context_for(*primary) if primary else None,
        tars_overlay_windows=[_context_for(*overlay) for overlay in overlays],
        observed_at=datetime.now(UTC),
    )


def resolve_window(target: str | None) -> tuple[int, str, str]:
    """Resolve a `target` (executable/title substring, or None/"current"/
    "screen"/"this" for what the user is actually looking at) to
    (hwnd, executable, window_title). Raises SkillExecutionError if nothing
    matches -- callers turn that into a real FAILED result, never a
    fabricated one.

    For the no-explicit-target case this deliberately does NOT return the
    literal GetForegroundWindow() result: TARS's own companion/orb window
    can itself be foreground (or always-on-top above the real content the
    user means), so it resolves via get_primary_visible_user_window()
    first, falling back to the raw foreground window only when no eligible
    user window was found at all (e.g. nothing but TARS is open)."""
    normalized = (target or "").strip().lower()
    if normalized in ("", "current", "foreground", "active", "screen", "this"):
        primary = get_primary_visible_user_window()
        if primary is not None:
            return primary
        raw = _raw_foreground_window()
        if raw is None:
            raise SkillExecutionError("no foreground window is available")
        return raw

    match = _find_window(target or "")
    if match is None:
        raise SkillExecutionError(f"no window found matching '{target}'")
    return match["hwnd"], match["executable"], match["window_title"]


def control_from_hwnd(hwnd: int) -> auto.Control:
    control = auto.ControlFromHandle(hwnd)
    if control is None:
        raise SkillExecutionError("failed to attach UI Automation to that window")
    return control


def capture_window_state(hwnd: int) -> WindowState:
    if _safe(lambda: win32gui.IsIconic(hwnd), False):
        return WindowState.minimized
    if _safe(lambda: win32gui.IsZoomed(hwnd), False):
        return WindowState.maximized
    if _safe(lambda: win32gui.IsWindow(hwnd), False):
        return WindowState.normal
    return WindowState.unknown


def capture_monitor(hwnd: int) -> MonitorInfo | None:
    def _get() -> MonitorInfo | None:
        handle = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        info = win32api.GetMonitorInfo(handle)
        left, top, right, bottom = info["Monitor"]
        return MonitorInfo(
            x=left,
            y=top,
            width=right - left,
            height=bottom - top,
            is_primary=bool(info.get("Flags", 0) & win32con.MONITORINFOF_PRIMARY),
        )

    return _safe(_get, None)


def capture_window_bounds(hwnd: int) -> WindowBounds | None:
    def _get() -> WindowBounds | None:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        return WindowBounds(x=left, y=top, width=right - left, height=bottom - top)

    return _safe(_get, None)


def capture_focused_control_info() -> FocusedControlInfo | None:
    def _get() -> FocusedControlInfo | None:
        control = auto.GetFocusedControl()
        if control is None:
            return None
        is_password = bool(_safe(lambda: control.IsPassword, False))
        return FocusedControlInfo(
            control_type=_safe(lambda: control.ControlTypeName, None),
            name=None if is_password else (_safe(lambda: control.Name, None) or None),
            automation_id=_safe(lambda: control.AutomationId, None) or None,
            class_name=_safe(lambda: control.ClassName, None) or None,
        )

    return _safe(_get, None)


def _context_for(hwnd: int, exe: str, title: str) -> ActiveWindowContext:
    """Shared rich-context builder for a resolved (hwnd, exe, title) triple
    -- used both by `build_active_window_context` (a single resolved
    window) and `get_visible_screen_state` (several windows snapshotted in
    one call, without re-resolving via `resolve_window`)."""
    return ActiveWindowContext(
        executable=exe or "unknown.exe",
        process_id=_safe(lambda: win32process.GetWindowThreadProcessId(hwnd)[1], None),
        window_title=title,
        window_bounds=capture_window_bounds(hwnd),
        captured_at=datetime.now(UTC),
        window_state=capture_window_state(hwnd),
        monitor=capture_monitor(hwnd),
        focused_control=capture_focused_control_info(),
        source=ContextSource.ui_automation,
    )


def build_active_window_context(target: str | None = None) -> ActiveWindowContext:
    """Full rich context snapshot for a window (default: the primary
    visible user window -- see resolve_window). Metadata only -- no
    screenshot, no control-tree, no text content. Used by
    `inspect_current_window`; distinct from the lightweight per-request
    `active_context` field a caller may already attach."""
    hwnd, exe, title = resolve_window(target)
    return _context_for(hwnd, exe, title)


def serialize_control(control: auto.Control) -> dict[str, Any]:
    is_password = bool(_safe(lambda: control.IsPassword, False))
    rect = _safe(lambda: control.BoundingRectangle, None)
    bounds = None
    if rect is not None and not _safe(rect.isempty, True):
        bounds = {
            "x": rect.left,
            "y": rect.top,
            "width": rect.width(),
            "height": rect.height(),
        }
    return {
        "control_type": _safe(lambda: control.ControlTypeName, "UnknownControl"),
        "name": None if is_password else (_safe(lambda: control.Name, None) or None),
        "automation_id": _safe(lambda: control.AutomationId, None) or None,
        "class_name": _safe(lambda: control.ClassName, None) or None,
        "is_enabled": bool(_safe(lambda: control.IsEnabled, True)),
        "is_password": is_password,
        "bounding_rectangle": bounds,
    }


def walk_actionable_controls(
    root: auto.Control,
    *,
    max_depth: int = _DEFAULT_MAX_DEPTH,
    max_controls: int = _DEFAULT_MAX_CONTROLS,
) -> tuple[list[auto.Control], bool]:
    """BFS from `root`'s children, bounded by depth and count. Returns
    (controls, truncated). Container types (Pane/Group/Window/...) are
    walked through but not themselves returned -- only types in
    ACTIONABLE_CONTROL_TYPES are collected, so the result stays a usable
    list of things worth acting on rather than the entire raw tree."""
    max_controls = max(1, min(max_controls, _HARD_MAX_CONTROLS))
    found: list[auto.Control] = []
    truncated = False
    queue: list[tuple[auto.Control, int]] = [(root, 0)]
    visited = 0
    visit_cap = max_controls * 20  # bound total tree walk, not just matches

    while queue and len(found) < max_controls:
        node, depth = queue.pop(0)
        visited += 1
        if visited > visit_cap:
            truncated = True
            break
        children = _safe(lambda n=node: n.GetChildren(), [])
        for child in children:
            control_type = _safe(lambda c=child: c.ControlTypeName, "")
            if control_type in ACTIONABLE_CONTROL_TYPES:
                found.append(child)
                if len(found) >= max_controls:
                    truncated = truncated or len(children) > 0
                    break
            if depth + 1 < max_depth:
                queue.append((child, depth + 1))
    if queue:
        truncated = True
    return found, truncated


def read_selected_text(control: auto.Control) -> str | None:
    """Returns selected text via TextPattern, or None if the control does
    not expose one. Password controls are refused (redacted), never read."""
    if _safe(lambda: bool(control.IsPassword), False):
        raise SkillExecutionError("refusing to read text from a password field")
    pattern = control.GetPattern(auto.PatternId.TextPattern)
    if pattern is None:
        return None
    ranges = _safe(lambda: pattern.GetSelection(), [])
    if not ranges:
        return ""
    text = "".join(_safe(lambda r=r: r.GetText(_MAX_TEXT_CHARS), "") for r in ranges)
    return text[:_MAX_TEXT_CHARS]


def read_clipboard_text() -> str | None:
    """Reads CF_UNICODETEXT from the clipboard. Returns None if the
    clipboard holds no text format (not an error -- a real, observable
    state). Raises SkillExecutionError only for a genuine access failure."""
    try:
        win32clipboard.OpenClipboard()
    except Exception as exc:
        raise SkillExecutionError(f"could not open clipboard: {exc}") from exc
    try:
        if not win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
            return None
        data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        return str(data)[:_MAX_TEXT_CHARS]
    finally:
        win32clipboard.CloseClipboard()


def do_focus(control: auto.Control) -> bool:
    return bool(control.SetFocus())


def do_invoke(control: auto.Control) -> None:
    invoke = control.GetPattern(auto.PatternId.InvokePattern)
    if invoke is not None:
        if invoke.Invoke():
            return
        raise SkillExecutionError("InvokePattern.Invoke() reported failure")
    toggle = control.GetPattern(auto.PatternId.TogglePattern)
    if toggle is not None:
        if toggle.Toggle():
            return
        raise SkillExecutionError("TogglePattern.Toggle() reported failure")
    legacy = control.GetPattern(auto.PatternId.LegacyIAccessiblePattern)
    if legacy is not None:
        if legacy.DoDefaultAction():
            return
        raise SkillExecutionError("LegacyIAccessiblePattern.DoDefaultAction() reported failure")
    raise SkillExecutionError(
        "control does not support Invoke, Toggle, or a legacy default action"
    )


def do_type(control: auto.Control, text: str, *, mode: str) -> None:
    if _safe(lambda: bool(control.IsPassword), False) and mode == "append":
        raise SkillExecutionError("append mode is not supported on password fields")

    value_pattern = control.GetPattern(auto.PatternId.ValuePattern)
    if value_pattern is not None:
        final_text = text
        if mode == "append":
            current = _safe(lambda: value_pattern.Value, "") or ""
            final_text = current + text
        if value_pattern.SetValue(final_text):
            return
        raise SkillExecutionError("ValuePattern.SetValue() reported failure")

    legacy = control.GetPattern(auto.PatternId.LegacyIAccessiblePattern)
    if legacy is not None:
        final_text = text
        if mode == "append":
            current = _safe(lambda: legacy.Value, "") or ""
            final_text = current + text
        if legacy.SetValue(final_text):
            return
        raise SkillExecutionError("LegacyIAccessiblePattern.SetValue() reported failure")

    raise SkillExecutionError("control does not support setting a text value")


def do_select(control: auto.Control) -> None:
    selection = control.GetPattern(auto.PatternId.SelectionItemPattern)
    if selection is not None:
        if selection.Select():
            return
        raise SkillExecutionError("SelectionItemPattern.Select() reported failure")
    legacy = control.GetPattern(auto.PatternId.LegacyIAccessiblePattern)
    if legacy is not None:
        # SELFLAG_TAKESELECTION | SELFLAG_TAKEFOCUS
        if legacy.Select(1 | 4):
            return
        raise SkillExecutionError("LegacyIAccessiblePattern.Select() reported failure")
    raise SkillExecutionError("control does not support selection")


_SCROLL_AXES = {
    "up": ("vertical", auto.ScrollAmount.SmallDecrement, auto.ScrollAmount.LargeDecrement),
    "down": ("vertical", auto.ScrollAmount.SmallIncrement, auto.ScrollAmount.LargeIncrement),
    "left": ("horizontal", auto.ScrollAmount.SmallDecrement, auto.ScrollAmount.LargeDecrement),
    "right": ("horizontal", auto.ScrollAmount.SmallIncrement, auto.ScrollAmount.LargeIncrement),
}


def _foreground_user_hwnd() -> int | None:
    """The real foreground window, or None if it is nothing or TARS's own companion/orb window."""
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd or not win32gui.IsWindowVisible(hwnd):
        return None
    try:
        title = win32gui.GetWindowText(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if _TARS_WINDOW_TITLE_RE.match(title) and _TARS_EXE_RE.match(_process_executable_name(pid)):
            return None
    except Exception:
        return None
    return hwnd


def _can_scroll(node: auto.Control, direction: str) -> bool:
    try:
        pattern = node.GetPattern(auto.PatternId.ScrollPattern)
        if pattern is None:
            return False
        return bool(pattern.HorizontallyScrollable if direction in ("left", "right") else pattern.VerticallyScrollable)
    except Exception:
        return False


def wheel_scroll(hwnd: int, *, direction: str, amount: str) -> None:
    """Real mouse-wheel input over the window's centre. Some surfaces (the Windows 11 Start/Search menu is a
    web UI) expose ScrollPattern elements that report 'not scrollable', so UI Automation cannot scroll them,
    yet the wheel does (verified by before/after pixels). The cursor is restored afterwards."""
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    notches = 8 if amount == "large" else 4
    delta = 120 * notches
    old = win32api.GetCursorPos()
    try:
        win32api.SetCursorPos(((left + right) // 2, (top + bottom) // 2))
        time.sleep(0.05)
        if direction in ("up", "down"):
            win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, delta if direction == "up" else -delta, 0)
        else:
            win32api.mouse_event(0x01000, 0, 0, delta if direction == "right" else -delta, 0)  # MOUSEEVENTF_HWHEEL
        time.sleep(0.15)
    finally:
        win32api.SetCursorPos(old)


def find_scrollable_descendant(control: auto.Control, *, max_depth: int = 15,
                               direction: str | None = None) -> auto.Control | None:
    """Bounded breadth-first search for the first descendant exposing
    ScrollPattern. Reproduced live (mission: P0 Trusted Desktop Control --
    "do not answer 'I don't have permission' for normal scrolling"): a
    real window's OWN top-level control frequently does not support
    ScrollPattern directly even when it plainly has scrollable content
    (confirmed against both a real Chrome window and the Start/Search
    surface) -- the actual ScrollPattern-supporting element is nested.
    Bounded (not unbounded) so a deep/wide tree can't hang this."""
    queue: list[tuple[auto.Control, int]] = [(control, 0)]
    seen = 0
    while queue and seen < 2000:
        node, depth = queue.pop(0)
        seen += 1
        if node.GetPattern(auto.PatternId.ScrollPattern) is not None and (
                direction is None or _can_scroll(node, direction)):
            return node
        if depth >= max_depth:
            continue
        try:
            children = node.GetChildren()
        except Exception:
            continue
        queue.extend((child, depth + 1) for child in children)
    return None


def do_scroll(control: auto.Control, *, direction: str, amount: str) -> None:
    scroll = control.GetPattern(auto.PatternId.ScrollPattern)
    if scroll is None:
        raise SkillExecutionError("control does not support scrolling")
    axis, small, large = _SCROLL_AXES[direction]
    move = large if amount == "large" else small
    no_amount = auto.ScrollAmount.NoAmount
    horizontal = move if axis == "horizontal" else no_amount
    vertical = move if axis == "vertical" else no_amount
    if not scroll.Scroll(horizontal, vertical):
        raise SkillExecutionError("ScrollPattern.Scroll() reported failure")


class ControlHandleCache:
    """Bounded, TTL'd map from an opaque `control_id` token to the UIA
    Control it was resolved from. Not a persistent store: entries expire
    (default 3 minutes) and the map is capped (default 400 entries, oldest
    evicted first) -- this is short-lived actionability state, not
    indefinite UI-tree retention."""

    def __init__(self, *, ttl_seconds: float = 180.0, max_entries: int = 400) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._entries: OrderedDict[str, tuple[float, auto.Control]] = OrderedDict()

    def put(self, control: auto.Control) -> str:
        self._sweep()
        if len(self._entries) >= self._max_entries:
            self._entries.popitem(last=False)
        token = uuid.uuid4().hex
        self._entries[token] = (time.monotonic() + self._ttl, control)
        return token

    def get(self, control_id: str) -> auto.Control | None:
        entry = self._entries.get(control_id)
        if entry is None:
            return None
        expires_at, control = entry
        if time.monotonic() > expires_at:
            self._entries.pop(control_id, None)
            return None
        return control

    def _sweep(self) -> None:
        now = time.monotonic()
        expired = [key for key, (expires_at, _) in self._entries.items() if now > expires_at]
        for key in expired:
            self._entries.pop(key, None)
