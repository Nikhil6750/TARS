from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable

import pytest

from app.action_contracts import (
    ActionRequest,
    ActionSource,
    ActionStatus,
    RiskLevel,
    SkillExecutionError,
    SkillValidationError,
)
from skills.desktop_control import DesktopControlSkill

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="UI Automation is Windows-only")

import uiautomation as auto  # noqa: E402
import win32gui  # noqa: E402


def _request(action: str, arguments: dict, source: ActionSource = ActionSource.hud) -> ActionRequest:
    return ActionRequest(skill="desktop_control", action=action, arguments=arguments, source=source)


def _find_control(root: auto.Control, predicate: Callable[[auto.Control], bool], *, max_depth: int = 8):
    def _walk(node: auto.Control, depth: int):
        try:
            if predicate(node):
                return node
        except Exception:
            pass
        if depth >= max_depth:
            return None
        for child in node.GetChildren():
            found = _walk(child, depth + 1)
            if found is not None:
                return found
        return None

    return _walk(root, 0)


def _enum_titled_windows(marker: str) -> list[int]:
    found: list[int] = []

    def _cb(hwnd: int, _extra: None) -> None:
        if win32gui.IsWindowVisible(hwnd) and marker in win32gui.GetWindowText(hwnd):
            found.append(hwnd)

    win32gui.EnumWindows(_cb, None)
    return found


def _find_hwnd_by_title_marker(marker: str, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = _enum_titled_windows(marker)
        if found:
            return found[0]
        time.sleep(0.2)
    raise TimeoutError(f"no window with marker {marker!r} appeared within {timeout}s")


def _close_tab_by_marker(hwnd: int, marker: str) -> None:
    """Modern Windows 11 Notepad is a single shared-process, multi-tab app:
    `notepad.exe <file>` activates an already-running host and the launcher
    stub process exits immediately, so it owns nothing to kill. Closing only
    this test's own tab (via its real 'Close Tab' UIA button) is the only
    safe teardown -- never touch the shared process, which may own the
    user's other, unrelated Notepad tabs."""
    try:
        root = auto.ControlFromHandle(hwnd)
        tab = _find_control(
            root, lambda c: marker in (c.Name or "") and c.ControlTypeName == "TabItemControl"
        )
        if tab is None:
            return
        close_button = _find_control(tab, lambda c: c.AutomationId == "CloseButton")
        if close_button is None:
            return
        invoke = close_button.GetPattern(auto.PatternId.InvokePattern)
        if invoke is not None:
            invoke.Invoke()
    except Exception:
        pass


@pytest.fixture
def notepad_tab():
    """Opens a real notepad.exe tab against a uniquely named temp file so
    tests can find and clean up precisely their own tab without disturbing
    any of the user's other open Notepad windows/tabs."""
    marker = f"tars_dc_test_{uuid.uuid4().hex[:8]}"
    path = os.path.join(tempfile.gettempdir(), f"{marker}.txt")
    with open(path, "w", encoding="utf-8"):
        pass
    subprocess.Popen(["notepad.exe", path])
    hwnd = _find_hwnd_by_title_marker(marker)
    try:
        yield hwnd, marker, path
    finally:
        _close_tab_by_marker(hwnd, marker)
        try:
            os.remove(path)
        except OSError:
            pass


# ---- classify_risk: read actions are READ_ONLY, focus/scroll are LOW_RISK,
# state-changing UI actions are CONFIRM_REQUIRED (never READ_ONLY).

def test_classify_risk_covers_every_capability():
    skill = DesktopControlSkill()
    assert skill.classify_risk("inspect_current_window", {}) == RiskLevel.READ_ONLY
    assert skill.classify_risk("list_controls", {}) == RiskLevel.READ_ONLY
    assert skill.classify_risk("read_selected_text", {}) == RiskLevel.READ_ONLY
    assert skill.classify_risk("read_clipboard", {}) == RiskLevel.READ_ONLY
    assert skill.classify_risk("focus_control", {}) == RiskLevel.LOW_RISK
    assert skill.classify_risk("scroll_control", {}) == RiskLevel.LOW_RISK
    assert skill.classify_risk("invoke_control", {}) == RiskLevel.CONFIRM_REQUIRED
    assert skill.classify_risk("type_into_control", {}) == RiskLevel.CONFIRM_REQUIRED
    assert skill.classify_risk("select_control", {}) == RiskLevel.CONFIRM_REQUIRED
    assert skill.classify_risk("delete_everything", {}) == RiskLevel.BLOCKED


# ---- validate(): pure argument-shape checks, no real window needed ----

async def test_validate_rejects_missing_control_id():
    skill = DesktopControlSkill()
    for action in ("focus_control", "invoke_control", "select_control", "scroll_control"):
        with pytest.raises(SkillValidationError):
            await skill.validate(action, {})
    with pytest.raises(SkillValidationError):
        await skill.validate("type_into_control", {"text": "hi"})


async def test_validate_type_into_control_requires_string_text():
    skill = DesktopControlSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("type_into_control", {"control_id": "x", "text": 123})


async def test_validate_type_into_control_rejects_oversized_text():
    skill = DesktopControlSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("type_into_control", {"control_id": "x", "text": "a" * 20_001})


async def test_validate_type_into_control_rejects_bad_mode():
    skill = DesktopControlSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate(
            "type_into_control", {"control_id": "x", "text": "hi", "mode": "overwrite"}
        )
    await skill.validate("type_into_control", {"control_id": "x", "text": "hi", "mode": "append"})


async def test_validate_scroll_control_requires_known_direction():
    skill = DesktopControlSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("scroll_control", {"control_id": "x", "direction": "sideways"})
    await skill.validate("scroll_control", {"control_id": "x", "direction": "up"})


# ---- mission: P0 Trusted Desktop Control -- "scroll down" must not require
# a pre-resolved control_id (section 8: "do NOT answer 'I don't have
# permission' for normal scrolling"). Mocked at the resolve_window/
# control_from_hwnd/do_scroll level (same pattern as test_skill_calculator.py)
# rather than another live window, since the fallback path itself --
# resolving "the current window" and calling do_scroll on it -- is already
# covered live by every other no-control_id action in this module.

async def test_validate_scroll_control_no_longer_requires_a_control_id():
    """'Scroll down' with nothing specific to scroll: omitting control_id
    entirely must pass validation (it used to require it)."""
    skill = DesktopControlSkill()
    await skill.validate("scroll_control", {"direction": "down"})


async def test_execute_scroll_control_falls_back_to_the_current_window_when_no_control_id_given():
    from unittest.mock import MagicMock, patch

    fake_window_control = MagicMock()
    skill = DesktopControlSkill()
    with patch("skills.desktop_control.resolve_window", return_value=(555, "notepad.exe", "Untitled - Notepad")) as resolve, \
         patch("skills.desktop_control.control_from_hwnd", return_value=fake_window_control) as from_hwnd, \
         patch("skills.desktop_control.do_scroll") as scroll:
        result = await skill.execute(_request("scroll_control", {"direction": "down"}))

    resolve.assert_called_once_with(None)  # "current window", same as every other no-target action
    from_hwnd.assert_called_once_with(555)
    scroll.assert_called_once_with(fake_window_control, direction="down", amount="small")
    assert result.status == ActionStatus.SUCCEEDED
    assert result.risk_level == RiskLevel.LOW_RISK


async def test_execute_scroll_control_fails_truthfully_when_nothing_is_in_view():
    from unittest.mock import patch

    skill = DesktopControlSkill()
    with patch("skills.desktop_control.resolve_window", side_effect=SkillExecutionError("no foreground window is available")):
        result = await skill.execute(_request("scroll_control", {"direction": "up"}))

    assert result.status == ActionStatus.FAILED
    assert "no foreground window" in (result.error or "")


async def test_execute_scroll_control_still_uses_an_explicit_control_id_when_given():
    """A caller-supplied control_id (from desktop_list_controls) must take
    priority over the current-window fallback -- the fallback is only for
    when nothing more specific was resolved."""
    from unittest.mock import MagicMock, patch

    skill = DesktopControlSkill()
    fake_control = MagicMock()
    control_id = skill._cache.put(fake_control)
    with patch("skills.desktop_control.resolve_window") as resolve, \
         patch("skills.desktop_control.do_scroll") as scroll:
        result = await skill.execute(_request("scroll_control", {"control_id": control_id, "direction": "down"}))

    resolve.assert_not_called()
    scroll.assert_called_once_with(fake_control, direction="down", amount="small")
    assert result.status == ActionStatus.SUCCEEDED


async def test_validate_list_controls_bounds_max_controls():
    skill = DesktopControlSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("list_controls", {"max_controls": 0})
    with pytest.raises(SkillValidationError):
        await skill.validate("list_controls", {"max_controls": 9999})
    await skill.validate("list_controls", {"max_controls": 50})


async def test_validate_inspect_current_window_bounds_max_controls():
    skill = DesktopControlSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("inspect_current_window", {"max_controls": 500})
    await skill.validate("inspect_current_window", {})


async def test_validate_rejects_unknown_action():
    skill = DesktopControlSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("delete_everything", {})


async def test_validate_read_clipboard_and_read_selected_text_accept_no_args():
    skill = DesktopControlSkill()
    await skill.validate("read_clipboard", {})
    await skill.validate("read_selected_text", {})


# ---- execute(): real Windows 11 Notepad verification ----

async def test_execute_list_controls_finds_real_notepad_controls(notepad_tab):
    _hwnd, marker, _path = notepad_tab
    skill = DesktopControlSkill()
    result = await skill.execute(_request("list_controls", {"target": marker}))

    assert result.status == ActionStatus.SUCCEEDED
    assert result.risk_level == RiskLevel.READ_ONLY
    control_types = {c["control_type"] for c in result.data["controls"]}
    assert "DocumentControl" in control_types
    assert all(c["control_id"] for c in result.data["controls"])


async def test_execute_list_controls_respects_max_controls_bound(notepad_tab):
    _hwnd, marker, _path = notepad_tab
    skill = DesktopControlSkill()
    result = await skill.execute(_request("list_controls", {"target": marker, "max_controls": 3}))
    assert result.status == ActionStatus.SUCCEEDED
    assert len(result.data["controls"]) <= 3


async def test_execute_type_into_control_sets_real_text(notepad_tab):
    _hwnd, marker, _path = notepad_tab
    skill = DesktopControlSkill()
    listed = await skill.execute(_request("list_controls", {"target": marker}))
    editors = [c for c in listed.data["controls"] if c["control_type"] == "DocumentControl"]
    assert editors, "expected notepad's text editor control to be discovered"

    result = await skill.execute(
        _request("type_into_control", {"control_id": editors[0]["control_id"], "text": "hello from tars"})
    )

    assert result.status == ActionStatus.SUCCEEDED
    assert result.risk_level == RiskLevel.CONFIRM_REQUIRED
    assert result.data["characters_written"] == len("hello from tars")


async def test_execute_invoke_control_add_new_tab_button(notepad_tab):
    hwnd, marker, _path = notepad_tab
    skill = DesktopControlSkill()
    listed = await skill.execute(
        _request("list_controls", {"target": marker, "max_controls": 200})
    )
    add_buttons = [
        c
        for c in listed.data["controls"]
        if c["control_type"] == "ButtonControl" and c["automation_id"] == "AddButton"
    ]
    assert add_buttons, "expected notepad's 'Add New Tab' button to be discovered"

    result = await skill.execute(
        _request("invoke_control", {"control_id": add_buttons[0]["control_id"]})
    )
    assert result.status == ActionStatus.SUCCEEDED
    assert result.risk_level == RiskLevel.CONFIRM_REQUIRED

    # This action opened a real, genuine extra "Untitled" tab -- close it
    # so the test doesn't leak state into the shared Notepad process.
    root = auto.ControlFromHandle(hwnd)
    new_tab = _find_control(
        root,
        lambda c: c.Name == "Untitled. Unmodified." and c.ControlTypeName == "TabItemControl",
    )
    if new_tab is not None:
        close_button = _find_control(new_tab, lambda c: c.AutomationId == "CloseButton")
        if close_button is not None:
            close_button.GetPattern(auto.PatternId.InvokePattern).Invoke()


async def test_execute_read_selected_text_reports_no_selection_honestly(notepad_tab):
    _hwnd, marker, _path = notepad_tab
    skill = DesktopControlSkill()
    listed = await skill.execute(_request("list_controls", {"target": marker}))
    editors = [c for c in listed.data["controls"] if c["control_type"] == "DocumentControl"]
    control_id = editors[0]["control_id"]

    result = await skill.execute(_request("read_selected_text", {"control_id": control_id}))

    assert result.status == ActionStatus.SUCCEEDED
    assert result.risk_level == RiskLevel.READ_ONLY
    assert result.data == {"selected_text": "", "has_selection": False}


async def test_execute_read_clipboard_returns_real_observable_state():
    skill = DesktopControlSkill()
    result = await skill.execute(_request("read_clipboard", {}))
    assert result.status == ActionStatus.SUCCEEDED
    assert result.risk_level == RiskLevel.READ_ONLY
    assert "has_text" in result.data
    assert "clipboard_text" in result.data


async def test_execute_inspect_current_window_returns_bounded_snapshot(notepad_tab):
    _hwnd, marker, _path = notepad_tab
    skill = DesktopControlSkill()
    result = await skill.execute(
        _request("inspect_current_window", {"target": marker, "max_controls": 10})
    )

    assert result.status == ActionStatus.SUCCEEDED
    assert result.data["active_window"]["executable"].lower() == "notepad.exe"
    assert marker in result.data["active_window"]["window_title"]
    assert result.data["active_window"]["window_state"] in ("normal", "minimized", "maximized")
    assert len(result.data["controls"]) <= 10
    # No clipboard content unless explicitly requested.
    assert "clipboard_text" not in result.data


async def test_execute_inspect_current_window_includes_clipboard_only_when_requested(notepad_tab):
    _hwnd, marker, _path = notepad_tab
    skill = DesktopControlSkill()
    result = await skill.execute(
        _request(
            "inspect_current_window",
            {"target": marker, "include_clipboard": True, "include_controls": False},
        )
    )
    assert result.status == ActionStatus.SUCCEEDED
    assert "clipboard_text" in result.data
    assert result.data["controls"] == []


async def test_execute_control_actions_fail_honestly_for_unknown_control_id():
    skill = DesktopControlSkill()
    for action, extra in (
        ("focus_control", {}),
        ("invoke_control", {}),
        ("select_control", {}),
        ("scroll_control", {"direction": "up"}),
        ("type_into_control", {"text": "x"}),
    ):
        with pytest.raises(SkillExecutionError):
            await skill.execute(_request(action, {"control_id": "not-a-real-id", **extra}))


async def test_execute_list_controls_reports_failure_for_unmatched_target():
    skill = DesktopControlSkill()
    with pytest.raises(SkillExecutionError):
        await skill.execute(
            _request("list_controls", {"target": f"no-such-window-{uuid.uuid4().hex}"})
        )


def test_find_scrollable_descendant_finds_a_nested_scroll_pattern():
    """Reproduced live: a real window's own top-level control frequently
    does not expose ScrollPattern directly (confirmed against both Chrome
    and the Start/Search surface) -- the search must walk into children,
    not just check the root."""
    from unittest.mock import MagicMock

    from skills._desktop_automation import find_scrollable_descendant

    grandchild = MagicMock()
    grandchild.GetPattern.return_value = object()  # supports ScrollPattern
    grandchild.GetChildren.return_value = []

    child_without = MagicMock()
    child_without.GetPattern.return_value = None
    child_without.GetChildren.return_value = [grandchild]

    root = MagicMock()
    root.GetPattern.return_value = None
    root.GetChildren.return_value = [child_without]

    found = find_scrollable_descendant(root)
    assert found is grandchild


def test_find_scrollable_descendant_gives_up_bounded_when_nothing_matches():
    from unittest.mock import MagicMock

    from skills._desktop_automation import find_scrollable_descendant

    leaf = MagicMock()
    leaf.GetPattern.return_value = None
    leaf.GetChildren.return_value = []

    root = MagicMock()
    root.GetPattern.return_value = None
    root.GetChildren.return_value = [leaf]

    assert find_scrollable_descendant(root) is None
