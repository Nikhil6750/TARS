"""`windows_app` skill -- launch, focus, and enumerate Windows applications.

`focus`/`list_running` use pywin32 (`win32gui`/`win32process`/`win32api`)
for real window enumeration and foreground-window switching -- there is no
mock/no-op fallback; if pywin32 is not importable this module raises at
import time (see the try/except below) rather than silently pretending to
focus a window it never touched.
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.action_contracts import (
    ActionRequest,
    ActionResult,
    ActionStatus,
    BaseSkill,
    RiskLevel,
    SkillExecutionError,
    SkillValidationError,
)

try:
    import win32api
    import win32con
    import win32gui
    import win32process
except ImportError as exc:  # pragma: no cover - exercised only off-Windows
    raise ImportError(
        "windows_app skill requires pywin32 (win32gui/win32process/win32con/win32api). "
        "Install it via apps/backend/requirements.txt (`pip install pywin32`)."
    ) from exc

_SW_RESTORE = win32con.SW_RESTORE
# PROCESS_QUERY_LIMITED_INFORMATION -- least-privilege access right that
# still allows reading the process's image path; works even for processes
# owned by other users, unlike PROCESS_QUERY_INFORMATION on some builds.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _process_executable_name(pid: int) -> str:
    """Best-effort executable basename for a PID. Returns "" if the process
    is gone or access is denied (e.g. a protected system process) -- never
    raises, since this is used for read-only enumeration/matching."""
    try:
        handle = win32api.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    except Exception:
        return ""
    try:
        path = win32process.GetModuleFileNameEx(handle, 0)
        return Path(path).name
    except Exception:
        return ""
    finally:
        try:
            win32api.CloseHandle(handle)
        except Exception:
            pass


def _enum_visible_windows() -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []

    def _callback(hwnd: int, _extra: None) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title.strip():
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            pid = None
        exe_name = _process_executable_name(pid) if pid else ""
        windows.append(
            {
                "hwnd": hwnd,
                "executable": exe_name,
                "window_title": title,
                "process_id": pid,
            }
        )

    win32gui.EnumWindows(_callback, None)
    return windows


def _find_window(target: str) -> dict[str, Any] | None:
    """Matches a running, visible window by executable basename (exact,
    case-insensitive) or by a case-insensitive substring of the window
    title -- whichever matches first."""
    target_lower = target.strip().lower()
    windows = _enum_visible_windows()

    for entry in windows:
        if entry["executable"] and entry["executable"].lower() == target_lower:
            return entry
    for entry in windows:
        if entry["executable"] and entry["executable"].lower() == f"{target_lower}.exe":
            return entry
    for entry in windows:
        if target_lower in entry["window_title"].lower():
            return entry
    return None


class WindowsAppSkill(BaseSkill):
    name = "windows_app"
    capabilities: tuple[str, ...] = ("launch", "focus", "list_running")

    def classify_risk(self, action: str, arguments: dict[str, Any]) -> RiskLevel:
        if action == "launch":
            return RiskLevel.LOW_RISK
        if action == "focus":
            return RiskLevel.LOW_RISK
        if action == "list_running":
            return RiskLevel.READ_ONLY
        return RiskLevel.BLOCKED

    async def validate(self, action: str, arguments: dict[str, Any]) -> None:
        if action == "launch":
            self._validate_launch_target(arguments)
        elif action == "focus":
            target = arguments.get("target")
            if not isinstance(target, str) or not target.strip():
                raise SkillValidationError("focus requires non-empty 'target'")
        elif action == "list_running":
            return
        else:
            raise SkillValidationError(f"unsupported windows_app action '{action}'")

    def _validate_launch_target(self, arguments: dict[str, Any]) -> None:
        target = arguments.get("target")
        if not isinstance(target, str) or not target.strip():
            raise SkillValidationError("launch requires non-empty 'target'")
        target = target.strip()
        path = Path(target)

        if path.is_absolute():
            if ".." in path.parts:
                raise SkillValidationError(f"path traversal not allowed in '{target}'")
            if path.suffix.lower() != ".exe":
                raise SkillValidationError(
                    f"absolute launch target must be an .exe path, got '{target}'"
                )
            if not path.is_file():
                raise SkillValidationError(f"launch target does not exist: '{target}'")
            return

        # Bare executable name -- must resolve via PATH, no separators or
        # traversal segments allowed (that would make it a disguised path).
        if ".." in target or "/" in target or "\\" in target:
            raise SkillValidationError(
                f"bare launch target must not contain path separators or '..': '{target}'"
            )
        if shutil.which(target) is None:
            raise SkillValidationError(f"'{target}' was not found on PATH")

    async def execute(self, request: ActionRequest) -> ActionResult:
        started = datetime.now(UTC)
        if request.action == "launch":
            return await self._execute_launch(request, started)
        if request.action == "focus":
            return self._execute_focus(request, started)
        if request.action == "list_running":
            return self._execute_list_running(request, started)
        raise SkillExecutionError(f"unsupported windows_app action '{request.action}'")

    async def _execute_launch(self, request: ActionRequest, started: datetime) -> ActionResult:
        target = request.arguments["target"].strip()
        path = Path(target)
        if path.is_absolute():
            argv = [str(path)]
        else:
            resolved = shutil.which(target)
            if resolved is None:
                raise SkillExecutionError(f"'{target}' was not found on PATH")
            argv = [resolved]

        try:
            process = subprocess.Popen(argv, shell=False)  # noqa: S603
        except OSError as exc:
            raise SkillExecutionError(f"failed to launch '{target}': {exc}") from exc

        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            f"Launched '{target}' (pid {process.pid}).",
            risk_level=RiskLevel.LOW_RISK,
            data={"target": target, "pid": process.pid},
            started_at=started,
        )

    def _execute_focus(self, request: ActionRequest, started: datetime) -> ActionResult:
        target = request.arguments["target"].strip()
        match = _find_window(target)
        if match is None:
            return self._result(
                request,
                ActionStatus.FAILED,
                f"No running window matched '{target}'.",
                risk_level=RiskLevel.LOW_RISK,
                error=f"no visible window found for target '{target}'",
                started_at=started,
            )

        hwnd = match["hwnd"]
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, _SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
        except Exception as exc:
            raise SkillExecutionError(f"failed to focus window for '{target}': {exc}") from exc

        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            f"Focused '{match['window_title']}' ({match['executable'] or 'unknown executable'}).",
            risk_level=RiskLevel.LOW_RISK,
            data={
                "target": target,
                "matched_executable": match["executable"],
                "matched_window_title": match["window_title"],
                "process_id": match["process_id"],
            },
            started_at=started,
        )

    def _execute_list_running(self, request: ActionRequest, started: datetime) -> ActionResult:
        windows = _enum_visible_windows()
        safe = [
            {
                "executable": entry["executable"],
                "window_title": entry["window_title"],
            }
            for entry in windows
        ]
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            f"Found {len(safe)} visible window(s).",
            risk_level=RiskLevel.READ_ONLY,
            data={"windows": safe},
            started_at=started,
        )
