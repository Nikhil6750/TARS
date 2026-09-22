"""`windows_app` skill -- resolve, launch, focus, close and enumerate Windows applications.

`launch`/`resolve` never take an executable name or path from the caller as the thing to run --
`skills.app_resolver.WindowsAppResolver` resolves a plain spoken app name (e.g. "clock",
"tradingview") against what is actually installed (Get-StartApps, Start Menu shortcuts, the
App Paths registry, aliases), so the assistant never has to guess a filename and a launch is
never silently substituted with a browser/web search. Every launch is window-verified
(`_verify_launch`) before reporting success; an already-running app is focused instead of
relaunched.

`focus`/`close`/`list_running` use pywin32 (`win32gui`/`win32process`/`win32api`) for real window
enumeration and foreground-window switching -- there is no mock/no-op fallback; if pywin32 is not
importable this module raises at import time (see the try/except below) rather than silently
pretending to focus a window it never touched.

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
import os
import subprocess
import time
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
from skills.app_resolver import AppRecord, get_resolver

logger = logging.getLogger("tars.windows_app")

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


def _snapshot_windows() -> set[tuple[int, str]]:
    return {(w["hwnd"], w["executable"]) for w in _enum_visible_windows()}


def _find_app_window(app: AppRecord) -> dict[str, Any] | None:
    """Same matching as `_find_window`, but against every name an AppRecord is known by
    (process names, display name, executable basename) instead of one raw string."""
    for candidate in (*app.process_names, app.display_name,
                      Path(app.executable).name if app.executable else ""):
        if not candidate:
            continue
        match = _find_window(candidate)
        if match:
            return match
    return None


def _focus_hwnd(hwnd: int) -> None:
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, _SW_RESTORE)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        win32gui.BringWindowToTop(hwnd)
        win32gui.ShowWindow(hwnd, _SW_RESTORE)


def _verify_launch(app: AppRecord, before: set[tuple[int, str]], timeout: float = 6.0) -> dict[str, Any] | None:
    """Polls for a new visible window that plausibly belongs to the just-launched app. Never
    reports SUCCESS on a launch command's return value alone (item 8: verify, don't assume)."""
    deadline = time.monotonic() + timeout
    name_tokens = [t for t in app.display_name.lower().split() if len(t) > 2]
    process_names_lower = {p.lower() for p in app.process_names}
    while time.monotonic() < deadline:
        for w in _enum_visible_windows():
            if (w["hwnd"], w["executable"]) in before:
                continue
            exe_lower = (w["executable"] or "").lower()
            title_lower = w["window_title"].lower()
            if exe_lower in process_names_lower or any(tok in title_lower for tok in name_tokens):
                return w
        time.sleep(0.25)
    return None


_CAPTURE_ACTIONS = {"capture_active_window", "get_monitors", "get_ui_elements"}
_DEFAULT_BRIDGE_TIMEOUT = 15.0


class WindowsAppSkill(BaseSkill):
    name = "windows_app"
    description = "Launch, focus, and enumerate Windows applications; capture the active window/monitors/UI elements."
    capabilities: tuple[str, ...] = (
        "launch",
        "focus",
        "close",
        "list_running",
        "resolve",
        "list_installed",
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
        if action in ("focus", "close"):
            return RiskLevel.LOW_RISK
        if action in ("list_running", "resolve", "list_installed", "capture_active_window",
                      "get_monitors", "get_ui_elements"):
            return RiskLevel.READ_ONLY
        return RiskLevel.BLOCKED

    async def validate(self, action: str, arguments: dict[str, Any]) -> None:
        if action == "launch":
            self._validate_launch_target(arguments)
        elif action in ("focus", "close"):
            target = arguments.get("target")
            if not isinstance(target, str) or not target.strip():
                raise SkillValidationError(f"{action} requires non-empty 'target'")
        elif action == "resolve":
            target = arguments.get("target")
            if not isinstance(target, str) or not target.strip():
                raise SkillValidationError("resolve requires non-empty 'target'")
        elif action in ("list_running", "list_installed"):
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
        """Only shape/safety validation here. Whether the target actually resolves to an
        installed application is a WindowsAppResolver question, answered in `_execute_launch` with
        a proper NOT_FOUND/AMBIGUOUS ActionResult -- not a validation rejection -- since spoken
        names like "clock" or "tradingview" are the expected input, not a path or PATH command."""
        target = arguments.get("target")
        if not isinstance(target, str) or not target.strip():
            raise SkillValidationError("launch requires non-empty 'target'")
        target = target.strip()
        path = Path(target)

        if path.is_absolute():
            # An explicit absolute path bypasses resolution entirely, so it still gets the strict
            # safety checks the old code always applied to it.
            if ".." in path.parts:
                raise SkillValidationError(f"path traversal not allowed in '{target}'")
            if path.suffix.lower() != ".exe":
                raise SkillValidationError(
                    f"absolute launch target must be an .exe path, got '{target}'"
                )
            if not path.is_file():
                raise SkillValidationError(f"launch target does not exist: '{target}'")
            return

        if ".." in target or "/" in target or "\\" in target:
            raise SkillValidationError(
                f"bare launch target must not contain path separators or '..': '{target}'"
            )

    async def execute(self, request: ActionRequest) -> ActionResult:
        started = datetime.now(UTC)
        if request.action == "launch":
            return await self._execute_launch(request, started)
        if request.action == "focus":
            return self._execute_focus(request, started)
        if request.action == "close":
            return self._execute_close(request, started)
        if request.action == "list_running":
            return self._execute_list_running(request, started)
        if request.action == "resolve":
            return self._execute_resolve(request, started)
        if request.action == "list_installed":
            return self._execute_list_installed(request, started)
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

        if path.is_absolute():
            # Explicit path: bypass resolution (already safety-checked in validate()), still
            # verify a window appears rather than trusting Popen's return alone.
            before = _snapshot_windows()
            try:
                process = subprocess.Popen([str(path)], shell=False)  # noqa: S603
            except OSError as exc:
                raise SkillExecutionError(f"failed to launch '{target}': {exc}") from exc
            app = AppRecord(id=target, display_name=path.stem, executable=str(path),
                            process_names=(path.name,), launch_method="exe", source="explicit_path")
            return self._launch_result(request, started, app, "SUCCESS", pid=process.pid,
                                       before=before, target=target)

        logger.info("[app_resolver] REQUEST=%r INTENT=OPEN_APP", target)
        result = get_resolver().resolve(target)
        if result.outcome == "NOT_FOUND":
            logger.info("[app_resolver] RESOLUTION=none RESULT=NOT_INSTALLED")
            return self._result(
                request, ActionStatus.FAILED, f"I couldn't find '{target}' installed.",
                risk_level=RiskLevel.LOW_RISK, error=f"not found: no installed application matches '{target}'",
                data={"outcome": "NOT_INSTALLED", "target": target}, started_at=started,
            )
        if result.outcome == "AMBIGUOUS":
            names = [c.display_name for c in result.candidates]
            logger.info("[app_resolver] RESOLUTION=ambiguous CANDIDATES=%r", names)
            return self._result(
                request, ActionStatus.FAILED,
                f"'{target}' matches more than one installed application: {', '.join(names)}. Which one?",
                risk_level=RiskLevel.LOW_RISK, error=f"ambiguous target '{target}'",
                data={"outcome": "AMBIGUOUS", "target": target, "candidates": names}, started_at=started,
            )

        assert result.app is not None  # guaranteed by outcome == "MATCH"
        app = result.app
        logger.info("[app_resolver] RESOLUTION=%r TYPE=%s LAUNCH_METHOD=%s", app.display_name,
                    app.app_type, app.launch_method)

        already = _find_app_window(app)
        if already is not None:
            _focus_hwnd(already["hwnd"])
            logger.info("[app_resolver] RESULT=ALREADY_RUNNING WINDOW=%r", already["window_title"])
            return self._launch_result(request, started, app, "ALREADY_RUNNING",
                                       window=already, target=target)

        before = _snapshot_windows()
        pid = None
        try:
            if app.launch_method == "exe" and app.executable:
                process = subprocess.Popen([app.executable], shell=False)  # noqa: S603
                pid = process.pid
            elif app.launch_method == "shortcut" and app.shortcut:
                os.startfile(app.shortcut)  # noqa: S606
            elif app.launch_method == "app_id":
                subprocess.Popen(  # noqa: S603
                    ["explorer.exe", f"shell:appsFolder\\{app.app_user_model_id}"], shell=False)
            else:
                raise SkillExecutionError(f"app '{app.display_name}' has no known launch method")
        except OSError as exc:
            raise SkillExecutionError(f"failed to launch '{app.display_name}': {exc}") from exc

        return self._launch_result(request, started, app, "SUCCESS", pid=pid, before=before, target=target)

    def _launch_result(self, request: ActionRequest, started: datetime, app: AppRecord, resolution: str,
                       *, target: str, pid: int | None = None, before: set[tuple[int, str]] | None = None,
                       window: dict[str, Any] | None = None) -> ActionResult:
        """Shared SUCCESS/ALREADY_RUNNING/FAILED shaping for a launch, always window-verified
        (item 8) rather than trusting a launch command's return value."""
        if window is None and before is not None:
            window = _verify_launch(app, before)
        if window is None and resolution == "SUCCESS":
            logger.info("[app_resolver] RESULT=FAILED (no window appeared)")
            return self._result(
                request, ActionStatus.FAILED, f"Launched {app.display_name} but no window appeared.",
                risk_level=RiskLevel.LOW_RISK, error=f"launch of '{app.display_name}' was not verified",
                data={"outcome": "FAILED", "target": target, "app_id": app.id}, started_at=started,
            )
        logger.info("[app_resolver] RESULT=%s WINDOW=%r", resolution, (window or {}).get("window_title"))
        verb = "switched to" if resolution == "ALREADY_RUNNING" else "opened"
        return self._result(
            request, ActionStatus.SUCCEEDED,
            f"{app.display_name} was already open; {verb} it." if resolution == "ALREADY_RUNNING"
            else f"Opened {app.display_name}.",
            risk_level=RiskLevel.LOW_RISK,
            data={
                "outcome": resolution, "target": target, "app_id": app.id,
                "display_name": app.display_name, "app_type": app.app_type,
                "launch_method": app.launch_method,
                "process_id": pid or (window or {}).get("process_id"),
                "window_title": (window or {}).get("window_title"),
            },
            started_at=started,
        )

    @staticmethod
    def _resolve_running_window(target: str) -> tuple[dict[str, Any] | None, AppRecord | None]:
        """Raw title/exe match first; falls back to the resolver (so "close MT5"/"switch to MT5"
        work even when the window title doesn't literally contain the alias)."""
        match = _find_window(target)
        if match is not None:
            return match, None
        result = get_resolver().resolve(target)
        if result.outcome == "MATCH" and result.app is not None:
            return _find_app_window(result.app), result.app
        return None, None

    def _execute_focus(self, request: ActionRequest, started: datetime) -> ActionResult:
        target = request.arguments["target"].strip()
        match, resolved_app = self._resolve_running_window(target)
        if match is None:
            return self._result(
                request,
                ActionStatus.FAILED,
                f"No running window matched '{target}'.",
                risk_level=RiskLevel.LOW_RISK,
                error=f"no visible window found for target '{target}'",
                started_at=started,
            )

        try:
            _focus_hwnd(match["hwnd"])
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
                "app_id": resolved_app.id if resolved_app else None,
            },
            started_at=started,
        )

    def _execute_close(self, request: ActionRequest, started: datetime) -> ActionResult:
        target = request.arguments["target"].strip()
        match, resolved_app = self._resolve_running_window(target)
        if match is None:
            return self._result(
                request, ActionStatus.FAILED, f"'{target}' is not running.",
                risk_level=RiskLevel.LOW_RISK, error=f"no running window found for target '{target}'",
                started_at=started,
            )

        hwnd = match["hwnd"]
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception as exc:
            raise SkillExecutionError(f"failed to close window for '{target}': {exc}") from exc

        closed = False
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if not win32gui.IsWindow(hwnd):
                closed = True
                break
            time.sleep(0.2)

        if not closed:
            return self._result(
                request, ActionStatus.FAILED,
                f"Asked '{match['window_title']}' to close, but it is still open (it may be prompting to save).",
                risk_level=RiskLevel.LOW_RISK, error=f"window for '{target}' did not close in time",
                data={"target": target, "matched_window_title": match["window_title"]}, started_at=started,
            )
        return self._result(
            request, ActionStatus.SUCCEEDED, f"Closed '{match['window_title']}'.",
            risk_level=RiskLevel.LOW_RISK,
            data={
                "target": target, "matched_executable": match["executable"],
                "matched_window_title": match["window_title"],
                "app_id": resolved_app.id if resolved_app else None,
            },
            started_at=started,
        )

    def _execute_resolve(self, request: ActionRequest, started: datetime) -> ActionResult:
        target = request.arguments["target"].strip()
        result = get_resolver().resolve(target)
        if result.outcome == "MATCH" and result.app is not None:
            return self._result(
                request, ActionStatus.SUCCEEDED, f"'{target}' resolves to {result.app.display_name}.",
                risk_level=RiskLevel.READ_ONLY,
                data={"outcome": "MATCH", "app": result.app.to_dict()}, started_at=started,
            )
        if result.outcome == "AMBIGUOUS":
            names = [c.display_name for c in result.candidates]
            return self._result(
                request, ActionStatus.SUCCEEDED, f"'{target}' matches more than one application: {', '.join(names)}.",
                risk_level=RiskLevel.READ_ONLY,
                data={"outcome": "AMBIGUOUS", "candidates": [c.to_dict() for c in result.candidates]},
                started_at=started,
            )
        return self._result(
            request, ActionStatus.SUCCEEDED, f"No installed application matches '{target}'.",
            risk_level=RiskLevel.READ_ONLY, data={"outcome": "NOT_INSTALLED"}, started_at=started,
        )

    def _execute_list_installed(self, request: ActionRequest, started: datetime) -> ActionResult:
        query = str(request.arguments.get("query") or "").strip().lower()
        records = get_resolver().discover()
        names = sorted({r.display_name for r in records.values()
                        if not query or query in r.display_name.lower()})
        return self._result(
            request, ActionStatus.SUCCEEDED, f"{len(names)} installed application(s) found.",
            risk_level=RiskLevel.READ_ONLY, data={"apps": names}, started_at=started,
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
