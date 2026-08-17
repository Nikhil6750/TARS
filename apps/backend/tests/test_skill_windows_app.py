from __future__ import annotations

import shutil
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.action_contracts import (
    ActionRequest,
    ActionSource,
    ActionStatus,
    RiskLevel,
    SkillValidationError,
)
from skills.windows_app import WindowsAppSkill, _find_window

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="pywin32/win32gui is Windows-only")

import win32con  # noqa: E402
import win32gui  # noqa: E402


@pytest.fixture
def real_window():
    """Creates a genuine, real top-level Win32 window (positioned
    off-screen so it doesn't visually disrupt the desktop) so `focus` and
    `list_running` can be tested against real win32gui/win32process calls
    instead of mocks."""
    title = f"TARSSkillTest_{uuid.uuid4().hex[:8]}"
    hwnd = win32gui.CreateWindow(
        "Static",
        title,
        win32con.WS_OVERLAPPEDWINDOW | win32con.WS_VISIBLE,
        -10000,
        -10000,
        200,
        200,
        0,
        0,
        0,
        None,
    )
    yield hwnd, title
    win32gui.DestroyWindow(hwnd)


def _request(action: str, arguments: dict) -> ActionRequest:
    return ActionRequest(
        skill="windows_app", action=action, arguments=arguments, source=ActionSource.hud
    )


def test_classify_risk():
    skill = WindowsAppSkill()
    assert skill.classify_risk("launch", {}) == RiskLevel.LOW_RISK
    assert skill.classify_risk("focus", {}) == RiskLevel.LOW_RISK
    assert skill.classify_risk("list_running", {}) == RiskLevel.READ_ONLY
    assert skill.classify_risk("delete_everything", {}) == RiskLevel.BLOCKED


async def test_validate_launch_rejects_empty_target():
    skill = WindowsAppSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("launch", {"target": ""})
    with pytest.raises(SkillValidationError):
        await skill.validate("launch", {})


async def test_validate_launch_rejects_path_traversal_in_bare_name():
    skill = WindowsAppSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("launch", {"target": "..\\evil.exe"})
    with pytest.raises(SkillValidationError):
        await skill.validate("launch", {"target": "sub/dir/app.exe"})


async def test_validate_launch_rejects_unresolvable_bare_name():
    skill = WindowsAppSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("launch", {"target": "definitely_not_a_real_executable_xyz123"})


async def test_validate_launch_accepts_real_path_resolvable_target():
    skill = WindowsAppSkill()
    assert shutil.which("cmd.exe") is not None
    await skill.validate("launch", {"target": "cmd.exe"})


async def test_validate_launch_accepts_absolute_existing_exe():
    skill = WindowsAppSkill()
    resolved = shutil.which("cmd.exe")
    assert resolved is not None
    await skill.validate("launch", {"target": resolved})


async def test_validate_launch_rejects_absolute_non_exe():
    skill = WindowsAppSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("launch", {"target": "C:\\Windows\\System32\\drivers\\etc\\hosts"})


async def test_validate_launch_rejects_absolute_missing_exe():
    skill = WindowsAppSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("launch", {"target": "C:\\definitely\\not\\real\\app.exe"})


async def test_validate_focus_requires_target():
    skill = WindowsAppSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("focus", {})
    await skill.validate("focus", {"target": "notepad"})


async def test_validate_list_running_accepts_empty_args():
    skill = WindowsAppSkill()
    await skill.validate("list_running", {})


async def test_validate_rejects_unknown_action():
    skill = WindowsAppSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("delete_everything", {})


async def test_execute_launch_uses_popen_without_shell():
    skill = WindowsAppSkill()
    request = _request("launch", {"target": "cmd.exe"})
    fake_process = MagicMock()
    fake_process.pid = 4242
    with patch("skills.windows_app.subprocess.Popen", return_value=fake_process) as mock_popen:
        result = await skill.execute(request)

    assert result.status == ActionStatus.SUCCEEDED
    assert result.data["pid"] == 4242
    args, kwargs = mock_popen.call_args
    assert kwargs.get("shell", False) is False
    assert isinstance(args[0], list)


async def test_execute_list_running_returns_real_windows(real_window):
    hwnd, title = real_window
    skill = WindowsAppSkill()
    request = _request("list_running", {})
    result = await skill.execute(request)

    assert result.status == ActionStatus.SUCCEEDED
    titles = [w["window_title"] for w in result.data["windows"]]
    assert title in titles


async def test_execute_focus_finds_and_focuses_real_window(real_window):
    hwnd, title = real_window
    skill = WindowsAppSkill()
    request = _request("focus", {"target": title})
    result = await skill.execute(request)

    assert result.status == ActionStatus.SUCCEEDED
    assert result.data["matched_window_title"] == title
    assert win32gui.GetForegroundWindow() == hwnd


async def test_execute_focus_reports_failure_when_no_window_matches():
    skill = WindowsAppSkill()
    request = _request("focus", {"target": f"no-such-window-{uuid.uuid4().hex}"})
    result = await skill.execute(request)

    assert result.status == ActionStatus.FAILED
    assert result.error is not None


def test_find_window_matches_by_title_substring(real_window):
    hwnd, title = real_window
    match = _find_window(title[:10])
    assert match is not None
    assert match["hwnd"] == hwnd
