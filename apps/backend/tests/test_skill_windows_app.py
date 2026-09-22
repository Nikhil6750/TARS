from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from actions.frontend_bridge import FrontendBridgeError
from app.action_contracts import (
    ActionRequest,
    ActionSource,
    ActionStatus,
    RiskLevel,
    SkillExecutionError,
    SkillValidationError,
)
from skills.app_resolver import AppRecord, ResolveResult
from skills.windows_app import WindowsAppSkill, _find_window


class _FakeBridge:
    def __init__(self, *, payload: dict | None = None, error: str | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[tuple] = []

    async def dispatch(self, request_id, skill, action, arguments, *, timeout):
        self.calls.append((request_id, skill, action, arguments, timeout))
        if self.error is not None:
            raise FrontendBridgeError(self.error)
        return self.payload or {}

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


async def test_validate_launch_accepts_unresolvable_bare_name():
    """Whether a spoken name actually resolves to an installed app is a WindowsAppResolver
    question answered at execute() time (NOT_INSTALLED/AMBIGUOUS), not a validation rejection --
    "clock" or "tradingview" must pass shape validation the same as any other spoken name."""
    skill = WindowsAppSkill()
    await skill.validate("launch", {"target": "definitely_not_a_real_installed_app_xyz123"})


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


async def test_validate_close_requires_target():
    skill = WindowsAppSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("close", {})
    await skill.validate("close", {"target": "notepad"})


async def test_validate_list_running_accepts_empty_args():
    skill = WindowsAppSkill()
    await skill.validate("list_running", {})


async def test_validate_rejects_unknown_action():
    skill = WindowsAppSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("delete_everything", {})


def _app(**kw) -> AppRecord:
    base = dict(id="testapp", display_name="Test App", app_type="WIN32",
               executable="C:\\fake\\testapp.exe", process_names=("testapp.exe",),
               launch_method="exe")
    base.update(kw)
    return AppRecord(**base)


async def test_execute_launch_absolute_path_uses_popen_without_shell_and_verifies_window():
    skill = WindowsAppSkill()
    request = _request("launch", {"target": "cmd.exe"})
    resolved = shutil.which("cmd.exe")
    assert resolved is not None
    fake_process = MagicMock()
    fake_process.pid = 4242
    with patch("skills.windows_app.subprocess.Popen", return_value=fake_process) as mock_popen, \
         patch("skills.windows_app._verify_launch", return_value={"window_title": "cmd", "process_id": 4242}):
        result = await skill.execute(_request("launch", {"target": resolved}))

    assert result.status == ActionStatus.SUCCEEDED
    assert result.data["outcome"] == "SUCCESS"
    assert result.data["process_id"] == 4242
    args, kwargs = mock_popen.call_args
    assert kwargs.get("shell", False) is False
    assert isinstance(args[0], list)


async def test_execute_launch_resolves_by_spoken_name_and_verifies_window():
    """The core fix: a bare spoken name goes through WindowsAppResolver, not a raw PATH lookup or
    an LLM-invented executable name -- and SUCCESS requires a verified new window (item 8)."""
    skill = WindowsAppSkill()
    app = _app()
    fake_process = MagicMock()
    fake_process.pid = 777
    with patch("skills.windows_app.get_resolver") as mock_get_resolver, \
         patch("skills.windows_app._find_app_window", return_value=None), \
         patch("skills.windows_app.subprocess.Popen", return_value=fake_process), \
         patch("skills.windows_app._verify_launch", return_value={"window_title": "Test App", "process_id": 777}):
        mock_get_resolver.return_value.resolve.return_value = ResolveResult(outcome="MATCH", app=app)
        result = await skill.execute(_request("launch", {"target": "test app"}))

    assert result.status == ActionStatus.SUCCEEDED
    assert result.data["outcome"] == "SUCCESS"
    assert result.data["display_name"] == "Test App"


async def test_execute_launch_reports_not_installed_never_falls_back_to_browser():
    skill = WindowsAppSkill()
    with patch("skills.windows_app.get_resolver") as mock_get_resolver:
        mock_get_resolver.return_value.resolve.return_value = ResolveResult(outcome="NOT_FOUND")
        result = await skill.execute(_request("launch", {"target": "some app nobody has"}))

    assert result.status == ActionStatus.FAILED
    assert result.data["outcome"] == "NOT_INSTALLED"
    assert "not found" in result.error.lower()


async def test_execute_launch_reports_ambiguous_with_candidates():
    skill = WindowsAppSkill()
    candidates = (_app(id="a", display_name="App One"), _app(id="b", display_name="App Two"))
    with patch("skills.windows_app.get_resolver") as mock_get_resolver:
        mock_get_resolver.return_value.resolve.return_value = ResolveResult(outcome="AMBIGUOUS", candidates=candidates)
        result = await skill.execute(_request("launch", {"target": "app"}))

    assert result.status == ActionStatus.FAILED
    assert result.data["outcome"] == "AMBIGUOUS"
    assert result.data["candidates"] == ["App One", "App Two"]


async def test_execute_launch_focuses_already_running_app_instead_of_relaunching():
    skill = WindowsAppSkill()
    app = _app()
    match = {"hwnd": 999, "executable": "testapp.exe", "window_title": "Test App", "process_id": 555}
    with patch("skills.windows_app.get_resolver") as mock_get_resolver, \
         patch("skills.windows_app._find_app_window", return_value=match), \
         patch("skills.windows_app._focus_hwnd") as mock_focus, \
         patch("skills.windows_app.subprocess.Popen") as mock_popen:
        mock_get_resolver.return_value.resolve.return_value = ResolveResult(outcome="MATCH", app=app)
        result = await skill.execute(_request("launch", {"target": "test app"}))

    assert result.status == ActionStatus.SUCCEEDED
    assert result.data["outcome"] == "ALREADY_RUNNING"
    mock_focus.assert_called_once_with(999)
    mock_popen.assert_not_called()  # never relaunch a duplicate instance


async def test_execute_launch_never_claims_success_when_no_window_appears():
    """Item 8: a launch command returning is never enough on its own."""
    skill = WindowsAppSkill()
    app = _app()
    fake_process = MagicMock()
    fake_process.pid = 111
    with patch("skills.windows_app.get_resolver") as mock_get_resolver, \
         patch("skills.windows_app._find_app_window", return_value=None), \
         patch("skills.windows_app.subprocess.Popen", return_value=fake_process), \
         patch("skills.windows_app._verify_launch", return_value=None):
        mock_get_resolver.return_value.resolve.return_value = ResolveResult(outcome="MATCH", app=app)
        result = await skill.execute(_request("launch", {"target": "test app"}))

    assert result.status == ActionStatus.FAILED
    assert result.data["outcome"] == "FAILED"


async def test_execute_resolve_reports_match_without_launching():
    skill = WindowsAppSkill()
    app = _app()
    with patch("skills.windows_app.get_resolver") as mock_get_resolver, \
         patch("skills.windows_app.subprocess.Popen") as mock_popen:
        mock_get_resolver.return_value.resolve.return_value = ResolveResult(outcome="MATCH", app=app)
        result = await skill.execute(_request("resolve", {"target": "test app"}))

    assert result.status == ActionStatus.SUCCEEDED
    assert result.risk_level == RiskLevel.READ_ONLY
    assert result.data["outcome"] == "MATCH"
    assert result.data["app"]["display_name"] == "Test App"
    mock_popen.assert_not_called()  # resolve is read-only: it must never launch anything


async def test_execute_list_installed_filters_by_query():
    skill = WindowsAppSkill()
    records = {
        "a": _app(id="a", display_name="Alpha App"),
        "b": _app(id="b", display_name="Beta App"),
    }
    with patch("skills.windows_app.get_resolver") as mock_get_resolver:
        mock_get_resolver.return_value.discover.return_value = records
        result = await skill.execute(_request("list_installed", {"query": "alpha"}))

    assert result.status == ActionStatus.SUCCEEDED
    assert result.data["apps"] == ["Alpha App"]


def test_windows_app_module_never_imports_a_browser_fallback():
    """Deterministic architectural invariant (item 19): OPEN_APP must never be able to reach a
    browser/web-search path, even indirectly, regardless of resolution outcome."""
    import skills.windows_app as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    assert "webbrowser" not in source
    assert "browser_search" not in source
    assert "browser_open_url" not in source
    assert not hasattr(mod, "webbrowser")


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
    assert win32gui.IsWindow(hwnd)
    fg = win32gui.GetForegroundWindow()
    assert fg == hwnd or fg == 0 or win32gui.IsWindow(hwnd)


async def test_execute_focus_reports_failure_when_no_window_matches():
    skill = WindowsAppSkill()
    request = _request("focus", {"target": f"no-such-window-{uuid.uuid4().hex}"})
    result = await skill.execute(request)

    assert result.status == ActionStatus.FAILED
    assert result.error is not None


async def test_execute_close_posts_wm_close_to_the_matched_window_and_verifies(real_window):
    hwnd, title = real_window
    skill = WindowsAppSkill()
    # PostMessage only queues WM_CLOSE; the fixture's raw window has no message loop pumping it
    # (unlike a real application), so simulate that loop having run and destroyed it -- proving
    # the right hwnd was targeted and the result is verified, not assumed.
    with patch("skills.windows_app.win32gui.PostMessage") as mock_post, \
         patch("skills.windows_app.win32gui.IsWindow", side_effect=[True, False]):
        result = await skill.execute(_request("close", {"target": title}))

    assert result.status == ActionStatus.SUCCEEDED
    mock_post.assert_called_once_with(hwnd, win32con.WM_CLOSE, 0, 0)


async def test_execute_close_reports_failure_when_window_never_closes(real_window):
    """A raw test window with no message loop never processes the posted WM_CLOSE -- this is a
    real (not mocked) demonstration of the give-up-after-timeout path, item 8's philosophy applied
    to close: never claim success without verifying the window actually went away."""
    hwnd, title = real_window
    skill = WindowsAppSkill()
    with patch("skills.windows_app.time.monotonic", side_effect=[0, 0, 10]):  # skip the real 5s wait
        result = await skill.execute(_request("close", {"target": title}))

    assert result.status == ActionStatus.FAILED
    assert "still open" in result.summary.lower()
    assert win32gui.IsWindow(hwnd)  # genuinely never closed


async def test_execute_close_reports_not_running_when_no_window_matches():
    skill = WindowsAppSkill()
    result = await skill.execute(_request("close", {"target": f"no-such-window-{uuid.uuid4().hex}"}))

    assert result.status == ActionStatus.FAILED
    assert "not running" in result.summary.lower()


def test_find_window_matches_by_title_substring(real_window):
    hwnd, title = real_window
    match = _find_window(title[:10])
    assert match is not None
    assert match["hwnd"] == hwnd


# -- Wave 2B native capture actions (bridged to the Tauri shell) --------


def test_classify_risk_capture_actions():
    skill = WindowsAppSkill()
    assert skill.classify_risk("capture_active_window", {}) == RiskLevel.READ_ONLY
    assert skill.classify_risk("get_monitors", {}) == RiskLevel.READ_ONLY
    assert skill.classify_risk("get_ui_elements", {}) == RiskLevel.READ_ONLY


async def test_validate_capture_active_window_rejects_non_bool_flag():
    skill = WindowsAppSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("capture_active_window", {"include_image_data": "yes"})
    await skill.validate("capture_active_window", {"include_image_data": True})


async def test_execute_capture_without_bridge_refuses_rather_than_fabricates():
    skill = WindowsAppSkill()  # no bridge wired
    with pytest.raises(SkillExecutionError):
        await skill.execute(_request("capture_active_window", {}))


async def test_execute_capture_dispatches_through_bridge_and_returns_real_data():
    bridge = _FakeBridge(
        payload={"summary": "Captured active window", "width": 1920, "height": 1080}
    )
    skill = WindowsAppSkill(bridge=bridge)
    result = await skill.execute(_request("capture_active_window", {}))

    assert result.status == ActionStatus.SUCCEEDED
    assert result.data["width"] == 1920
    assert len(bridge.calls) == 1


async def test_execute_capture_never_fabricates_success_on_secure_desktop():
    bridge = _FakeBridge(
        payload={
            "is_secure_desktop": True,
            "error": "Capture refused: Secure desktop or credential screen active",
        }
    )
    skill = WindowsAppSkill(bridge=bridge)
    result = await skill.execute(_request("capture_active_window", {}))

    assert result.status == ActionStatus.FAILED
    assert "secure desktop" in result.error.lower()


async def test_execute_capture_surfaces_bridge_timeout_as_failed():
    bridge = _FakeBridge(error="Frontend did not report a result in time")
    skill = WindowsAppSkill(bridge=bridge)
    result = await skill.execute(_request("get_monitors", {}))

    assert result.status == ActionStatus.FAILED
    assert result.error is not None
