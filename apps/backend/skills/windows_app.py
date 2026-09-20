"""`windows_app` skill -- launch, focus, and enumerate Windows applications.

`focus`/`list_running` use pywin32 (`win32gui`/`win32process`/`win32api`)
for real window enumeration and foreground-window switching -- there is no
mock/no-op fallback; if pywin32 is not importable this module raises at
import time (see the try/except below) rather than silently pretending to
focus a window it never touched.

Wave 2B adds `capture_active_window`/`get_monitors`/`get_ui_elements`.
Screen/monitor pixel capture is a native (Rust/Tauri) capability -- Antigravity's
`apps/web/src-tauri/src/lib.rs` implements the real Win32 capture, including
secure-desktop refusal (`is_secure_desktop_window`). This backend cannot
grab pixels itself, so these three actions are validated/risk-classified
here like any other action, then physically carried out via
`actions.frontend_bridge.FrontendCommandBridge`, which dispatches the
already-authorized command to the connected native shell and waits for its
real, truthful report -- including a refused secure-desktop capture, which
must surface as FAILED here, never as a fabricated SUCCEEDED.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from actions.frontend_bridge import FrontendBridgeError, FrontendCommandBridge
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

logger = logging.getLogger("tars.skills.windows_app")

_SW_RESTORE = win32con.SW_RESTORE
# PROCESS_QUERY_LIMITED_INFORMATION -- least-privilege access right that
# still allows reading the process's image path; works even for processes
# owned by other users, unlike PROCESS_QUERY_INFORMATION on some builds.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


# shell:appsFolder enumeration walks every installed app (Store/MSIX plus
# classic Start Menu entries) via COM and was physically measured to cost
# 1.7-3s under real system load (vs. sub-100ms with little else running) --
# far too slow to repeat on every "open X". The installed app set doesn't
# change mid-session, so the whole listing is enumerated at most once per
# process lifetime and cached; every lookup after that (any target, found
# or not) is an in-memory scan, effectively free. `warm_appsfolder_cache()`
# lets the app trigger that one-time cost proactively at startup, off the
# request path entirely.
_appsfolder_listing_cache: list[tuple[str, str]] | None = None


def _load_appsfolder_listing() -> list[tuple[str, str]]:
    global _appsfolder_listing_cache
    if _appsfolder_listing_cache is not None:
        return _appsfolder_listing_cache

    listing: list[tuple[str, str]] = []
    try:
        import win32com.client

        shell = win32com.client.Dispatch("Shell.Application")
        namespace = shell.NameSpace("shell:appsFolder")
        if namespace is not None:
            for item in namespace.Items():
                name = str(item.Name or "")
                if not name:
                    continue
                aumid = item.ExtendedProperty("System.AppUserModel.ID")
                if aumid:
                    listing.append((name.lower(), str(aumid)))
    except Exception:
        logger.exception("_load_appsfolder_listing: shell:appsFolder enumeration failed")

    _appsfolder_listing_cache = listing
    return listing


def warm_appsfolder_cache() -> None:
    """Pay the one-time shell:appsFolder enumeration cost proactively
    (call from a background thread at startup) so the first real "open X"
    voice command during a session doesn't have to pay it inline."""
    _load_appsfolder_listing()


def _resolve_appsfolder_target(target: str) -> str | None:
    """Best-effort AUMID (Application User Model ID) lookup for a bare
    launch target that isn't a plain PATH executable. Many real installed
    apps -- confirmed on this machine for Calculator, Notepad, and
    TradingView -- are Store/MSIX-packaged and only resolvable through the
    same `shell:appsFolder` namespace the Start Menu's own search uses, not
    `shutil.which()`. Matches by exact display name first, then a
    case-insensitive substring. Returns None on no match (never raises --
    this augments, never replaces, the PATH check)."""
    target_lower = target.strip().lower()
    listing = _load_appsfolder_listing()
    for name_lower, aumid in listing:
        if name_lower == target_lower:
            return aumid
    for name_lower, aumid in listing:
        if target_lower in name_lower:
            return aumid
    return None


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
        try:
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
        except Exception:
            return

    try:
        win32gui.EnumWindows(_callback, None)
    except Exception:
        pass
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


_CAPTURE_ACTIONS = {"capture_active_window", "get_monitors", "get_ui_elements"}
_DEFAULT_BRIDGE_TIMEOUT = 15.0


class WindowsAppSkill(BaseSkill):
    name = "windows_app"
    description = "Launch, focus, and enumerate Windows applications; capture the active window/monitors/UI elements."
    capabilities: tuple[str, ...] = (
        "launch",
        "focus",
        "list_running",
        "capture_active_window",
        "get_monitors",
        "get_ui_elements",
    )

    def __init__(self, bridge: FrontendCommandBridge | None = None) -> None:
        self._bridge = bridge

    async def health(self) -> dict[str, Any]:
        # launch/focus/list_running work with no bridge (pure win32); only
        # the capture actions need one -- report both facts rather than one
        # blanket true/false.
        return {
            "available": True,
            "capture_actions_available": self._bridge is not None,
            "requires_for_capture": "FrontendCommandBridge",
        }

    def classify_risk(self, action: str, arguments: dict[str, Any]) -> RiskLevel:
        if action == "launch":
            return RiskLevel.LOW_RISK
        if action == "focus":
            return RiskLevel.LOW_RISK
        if action in ("list_running", "capture_active_window", "get_monitors", "get_ui_elements"):
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
        elif action == "capture_active_window":
            include_image = arguments.get("include_image_data")
            if include_image is not None and not isinstance(include_image, bool):
                raise SkillValidationError("'include_image_data' must be a boolean")
        elif action in ("get_monitors", "get_ui_elements"):
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
        if shutil.which(target) is not None:
            return
        if _resolve_appsfolder_target(target) is not None:
            return
        raise SkillValidationError(f"'{target}' was not found on PATH or as an installed app")

    async def execute(self, request: ActionRequest) -> ActionResult:
        started = datetime.now(UTC)
        if request.action == "launch":
            return await self._execute_launch(request, started)
        if request.action == "focus":
            return self._execute_focus(request, started)
        if request.action == "list_running":
            return self._execute_list_running(request, started)
        if request.action in _CAPTURE_ACTIONS:
            return await self._execute_capture(request, started)
        raise SkillExecutionError(f"unsupported windows_app action '{request.action}'")

    async def _execute_capture(self, request: ActionRequest, started: datetime) -> ActionResult:
        if self._bridge is None:
            raise SkillExecutionError(
                f"windows_app.{request.action}() requires a connected native shell and no "
                "FrontendCommandBridge is wired -- refusing rather than fabricating a result"
            )
        try:
            data = await self._bridge.dispatch(
                request.id,
                self.name,
                request.action,
                request.arguments,
                timeout=_DEFAULT_BRIDGE_TIMEOUT,
            )
        except FrontendBridgeError as exc:
            return self._result(
                request,
                ActionStatus.FAILED,
                f"windows_app.{request.action}() did not complete: {exc}",
                risk_level=RiskLevel.READ_ONLY,
                error=str(exc),
                started_at=started,
            )
        if data.get("is_secure_desktop"):
            return self._result(
                request,
                ActionStatus.FAILED,
                "Capture refused: secure desktop or credential screen active.",
                risk_level=RiskLevel.READ_ONLY,
                data=data,
                error=data.get("error") or "secure desktop capture refused",
                started_at=started,
            )
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            str(data.get("summary") or f"Executed windows_app.{request.action}()."),
            risk_level=RiskLevel.READ_ONLY,
            data=data,
            started_at=started,
        )

    async def _execute_launch(self, request: ActionRequest, started: datetime) -> ActionResult:
        target = request.arguments["target"].strip()
        path = Path(target)
        resolved_via = "absolute_path"
        aumid: str | None = None
        if path.is_absolute():
            argv = [str(path)]
        else:
            resolved = shutil.which(target)
            if resolved is not None:
                resolved_via = "path"
                argv = [resolved]
            else:
                aumid = _resolve_appsfolder_target(target)
                if aumid is None:
                    raise SkillExecutionError(f"'{target}' was not found on PATH or as an installed app")
                # Store/MSIX-packaged apps have no PATH executable to spawn
                # directly; `explorer.exe shell:appsFolder\<AUMID>` is the
                # standard, non-elevated way to activate one from a plain
                # subprocess (physically verified: Calculator's real
                # CalculatorApp process starts this way on this machine).
                resolved_via = "appsFolder"
                argv = ["explorer.exe", f"shell:appsFolder\\{aumid}"]

        try:
            process = subprocess.Popen(argv, shell=False)  # noqa: S603
        except OSError as exc:
            raise SkillExecutionError(f"failed to launch '{target}': {exc}") from exc

        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            f"Launched '{target}' (pid {process.pid}).",
            risk_level=RiskLevel.LOW_RISK,
            data={
                "target": target,
                "pid": process.pid,
                "resolved_via": resolved_via,
                "aumid": aumid,
            },
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
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                win32gui.BringWindowToTop(hwnd)
                win32gui.ShowWindow(hwnd, _SW_RESTORE)
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
