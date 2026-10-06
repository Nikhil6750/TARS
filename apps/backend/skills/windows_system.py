"""`windows_system` skill -- native, deterministic laptop control: volume, microphone mute,
brightness, media keys, battery, window management and (confirmation-gated) power actions.

Everything goes through native Windows APIs (Core Audio via pycaw, WMI brightness, the system
media-key events, win32gui, psutil) -- never screenshots or mouse clicks. Ordinary reversible
hardware actions are LOW_RISK (no confirmation); shutdown/restart/sleep are CONFIRM_REQUIRED and
only ever run the one exact operation that was confirmed.
"""
from __future__ import annotations

import asyncio
import subprocess
import time
from datetime import UTC, datetime
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

_READ = {"get_volume", "get_microphone_mute", "get_brightness", "get_battery_status"}
_REVERSIBLE = {
    "set_volume", "volume_up", "volume_down", "mute_audio", "unmute_audio",
    "mute_microphone", "unmute_microphone",
    "set_brightness", "brightness_up", "brightness_down",
    "media_play_pause", "media_next", "media_previous", "media_stop",
    "window_minimize", "window_maximize", "window_restore", "window_switch",
}
_POWER = {"shutdown", "restart", "sleep"}
_CONSEQUENTIAL_WINDOW = {"window_close"}  # may discard unsaved work

_MEDIA_VK = {"media_play_pause": 0xB3, "media_next": 0xB0, "media_previous": 0xB1, "media_stop": 0xB2}
_DEFAULT_STEP = 10


def _clamp(value: float) -> int:
    return max(0, min(100, int(round(value))))


def _num(value: Any) -> Any:
    """Gemini sometimes sends numbers as strings ("10"); accept those, never bools."""
    if isinstance(value, str):
        try:
            return float(value.strip().rstrip("%"))
        except ValueError:
            return value
    return value


def _percent(arguments: dict[str, Any], key: str = "percent") -> int:
    value = _num(arguments.get(key))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SkillValidationError(f"'{key}' must be a number between 0 and 100")
    if not 0 <= value <= 100:
        raise SkillValidationError(f"'{key}' must be between 0 and 100")
    return int(round(value))


def _step(arguments: dict[str, Any]) -> int:
    value = _num(arguments.get("step", _DEFAULT_STEP))
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 100:
        raise SkillValidationError("'step' must be between 1 and 100")
    return int(round(value))


# ---- native backends (module-level so tests can substitute them) -------------------------

def _speakers():
    from pycaw.pycaw import AudioUtilities

    return AudioUtilities.GetSpeakers().EndpointVolume


def _microphone():
    import comtypes
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

    dev = AudioUtilities.GetMicrophone()
    if dev is None:
        raise SkillExecutionError("no microphone endpoint found")
    iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return iface.QueryInterface(IAudioEndpointVolume) if hasattr(iface, "QueryInterface") else \
        comtypes.cast(iface, comtypes.POINTER(IAudioEndpointVolume))


def _get_volume() -> tuple[int, bool]:
    ep = _speakers()
    return _clamp(ep.GetMasterVolumeLevelScalar() * 100), bool(ep.GetMute())


def _set_volume(percent: int) -> None:
    _speakers().SetMasterVolumeLevelScalar(percent / 100.0, None)


def _set_mute(muted: bool) -> None:
    _speakers().SetMute(1 if muted else 0, None)


def _get_mic_mute() -> bool:
    return bool(_microphone().GetMute())


def _set_mic_mute(muted: bool) -> None:
    _microphone().SetMute(1 if muted else 0, None)


def _wmi():
    import win32com.client

    return win32com.client.GetObject("winmgmts:root\\WMI")


def _get_brightness() -> int | None:
    try:
        for monitor in _wmi().InstancesOf("WmiMonitorBrightness"):
            if monitor.Active:
                return int(monitor.CurrentBrightness)
    except Exception:
        return None
    return None


def _set_brightness(percent: int) -> bool:
    try:
        methods = list(_wmi().InstancesOf("WmiMonitorBrightnessMethods"))
        if not methods:
            return False
        wmi = _wmi()
        for m in methods:
            # Direct m.WmiSetBrightness(...) is rejected by pywin32 ("Invalid parameter"); the
            # explicit in-parameter object is the form WMI accepts.
            params = m.Methods_("WmiSetBrightness").InParameters.SpawnInstance_()
            params.Properties_("Timeout").Value = 1
            params.Properties_("Brightness").Value = percent
            wmi.ExecMethod(m.Path_.Path, "WmiSetBrightness", params)
        return True
    except Exception:
        return False


def _press_media_key(vk: int) -> None:
    import win32api
    import win32con

    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_EXTENDEDKEY, 0)
    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_EXTENDEDKEY | win32con.KEYEVENTF_KEYUP, 0)


def _battery():
    import psutil

    return psutil.sensors_battery()


def _foreground_hwnd() -> int:
    import win32gui

    return win32gui.GetForegroundWindow()


def _show_window(hwnd: int, command: int) -> None:
    import win32gui

    win32gui.ShowWindow(hwnd, command)


def _is_tars_window(hwnd: int) -> bool:
    """TARS's own companion/orb windows must never be minimised/closed by a voice command."""
    try:
        import win32gui
        import win32process
        import psutil

        title = win32gui.GetWindowText(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return title.startswith("TARS") and "antigravity" not in title.lower() and             psutil.Process(pid).name().lower().startswith("tars")
    except Exception:
        return False


def _close_window(hwnd: int) -> None:
    import win32con
    import win32gui

    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)


def _alt_tab() -> None:
    import win32api
    import win32con

    VK_MENU, VK_TAB = 0x12, 0x09
    win32api.keybd_event(VK_MENU, 0, 0, 0)
    win32api.keybd_event(VK_TAB, 0, 0, 0)
    win32api.keybd_event(VK_TAB, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)


def _run_power(action: str) -> None:
    if action == "shutdown":
        subprocess.Popen(["shutdown", "/s", "/t", "0"])
    elif action == "restart":
        subprocess.Popen(["shutdown", "/r", "/t", "0"])
    else:
        subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])


class WindowsSystemSkill(BaseSkill):
    name = "windows_system"
    description = ("Native laptop control: volume, mute, microphone mute, brightness, media keys, battery, "
                   "window management; power actions require confirmation.")
    capabilities: tuple[str, ...] = tuple(sorted(_READ | _REVERSIBLE | _POWER | _CONSEQUENTIAL_WINDOW))

    def classify_risk(self, action: str, arguments: dict[str, Any]) -> RiskLevel:
        if action in _READ:
            return RiskLevel.READ_ONLY
        if action in _REVERSIBLE:
            return RiskLevel.LOW_RISK
        if action in _POWER or action in _CONSEQUENTIAL_WINDOW:
            return RiskLevel.CONFIRM_REQUIRED
        return RiskLevel.BLOCKED

    async def validate(self, action: str, arguments: dict[str, Any]) -> None:
        if action not in self.capabilities:
            raise SkillValidationError(f"unsupported windows_system action '{action}'")
        if action in {"set_volume", "set_brightness"}:
            _percent(arguments)
        elif action in {"volume_up", "volume_down", "brightness_up", "brightness_down"} and "step" in arguments:
            _step(arguments)

    async def execute(self, request: ActionRequest) -> ActionResult:
        started = datetime.now(UTC)
        action, args = request.action, request.arguments
        risk = self.classify_risk(action, args)
        t0 = time.perf_counter()
        try:
            status, summary, data = await asyncio.to_thread(self._run_com, action, args)
        except SkillExecutionError:
            raise
        except Exception as exc:  # native API failure is reported, never faked
            raise SkillExecutionError(f"{action} failed: {type(exc).__name__}: {exc}") from exc
        data = {**data, "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
        return self._result(request, status, summary, risk_level=risk, data=data, started_at=started,
                            error=None if status is ActionStatus.SUCCEEDED else summary)

    # ------------------------------------------------------------------------------------
    def _run_com(self, action: str, a: dict[str, Any]) -> tuple[ActionStatus, str, dict]:
        """Core Audio and WMI are COM: a thread-pool thread is not COM-initialized, so initialise
        explicitly per call (otherwise it only works when a pool thread happened to be initialised)."""
        import pythoncom

        pythoncom.CoInitialize()
        try:
            return self._run(action, a)
        finally:
            pythoncom.CoUninitialize()

    def _run(self, action: str, a: dict[str, Any]) -> tuple[ActionStatus, str, dict]:
        ok, bad = ActionStatus.SUCCEEDED, ActionStatus.FAILED
        if action == "get_volume":
            vol, muted = _get_volume()
            return ok, f"Volume is {vol} percent{', muted' if muted else ''}.", {"volume": vol, "muted": muted}
        if action in {"set_volume", "volume_up", "volume_down"}:
            current, _ = _get_volume()
            target = (_percent(a) if action == "set_volume"
                      else _clamp(current + (_step(a) if action == "volume_up" else -_step(a))))
            if target > 0:
                _set_mute(False)
            _set_volume(target)
            vol, muted = _get_volume()
            verified = abs(vol - target) <= 2
            return (ok if verified else bad,
                    f"Volume is now {vol} percent." if verified else f"Volume read back as {vol}, expected {target}.",
                    {"volume": vol, "requested": target, "verified": verified, "muted": muted})
        if action in {"mute_audio", "unmute_audio"}:
            want = action == "mute_audio"
            _set_mute(want)
            _vol, muted = _get_volume()
            return (ok if muted == want else bad, "Audio muted." if muted else "Audio unmuted.",
                    {"muted": muted, "verified": muted == want})
        if action == "get_microphone_mute":
            muted = _get_mic_mute()
            return ok, f"The microphone is {'muted' if muted else 'live'}.", {"muted": muted}
        if action in {"mute_microphone", "unmute_microphone"}:
            want = action == "mute_microphone"
            _set_mic_mute(want)
            muted = _get_mic_mute()
            return (ok if muted == want else bad, "Microphone muted." if muted else "Microphone unmuted.",
                    {"muted": muted, "verified": muted == want})
        if action == "get_brightness":
            b = _get_brightness()
            if b is None:
                return ActionStatus.FAILED, "UNSUPPORTED: this display does not expose programmatic brightness.", \
                    {"outcome": "UNSUPPORTED"}
            return ok, f"Brightness is {b} percent.", {"brightness": b}
        if action in {"set_brightness", "brightness_up", "brightness_down"}:
            current = _get_brightness()
            if current is None:
                return bad, "UNSUPPORTED: this display/driver does not expose programmatic brightness.", \
                    {"outcome": "UNSUPPORTED"}
            target = (_percent(a) if action == "set_brightness"
                      else _clamp(current + (_step(a) if action == "brightness_up" else -_step(a))))
            if not _set_brightness(target):
                return bad, "UNSUPPORTED: Windows rejected the brightness change.", {"outcome": "UNSUPPORTED"}
            now = None
            for _ in range(8):  # panels ramp; poll the readback instead of one long sleep
                time.sleep(0.1)
                now = _get_brightness()
                if now is not None and abs(now - target) <= 5:
                    break
            verified = now is not None and abs(now - target) <= 5
            return (ok if verified else bad,
                    f"Brightness is now {now} percent." if verified else f"Brightness read back as {now}, expected {target}.",
                    {"brightness": now, "requested": target, "verified": verified})
        if action in _MEDIA_VK:
            _press_media_key(_MEDIA_VK[action])
            # Windows exposes no synchronous ack for a media key; report dispatch honestly.
            return ok, {"media_play_pause": "Toggled play and pause.", "media_next": "Skipped to the next track.",
                        "media_previous": "Went to the previous track.", "media_stop": "Stopped playback."}[action], \
                {"dispatched": True, "verified": False}
        if action == "get_battery_status":
            b = _battery()
            if b is None:
                return ok, "No battery detected; this machine is on mains power.", {"battery": None}
            return ok, f"Battery is at {int(b.percent)} percent, {'charging' if b.power_plugged else 'on battery'}.", \
                {"percent": int(b.percent), "plugged_in": bool(b.power_plugged)}
        if action in {"window_minimize", "window_maximize", "window_restore", "window_close"}:
            import win32con

            hwnd = _foreground_hwnd()
            if not hwnd:
                return bad, "There is no foreground window.", {}
            if _is_tars_window(hwnd):
                return bad, "The window in front is TARS itself, so I left it alone.", {"outcome": "REFUSED_SELF"}
            if action == "window_close":
                _close_window(hwnd)
            else:
                _show_window(hwnd, {"window_minimize": win32con.SW_MINIMIZE, "window_maximize": win32con.SW_MAXIMIZE,
                                    "window_restore": win32con.SW_RESTORE}[action])
            return ok, f"Done: {action.replace('_', ' ')}.", {"hwnd": hwnd}
        if action == "window_switch":
            before = _foreground_hwnd()
            _alt_tab()
            time.sleep(0.25)
            after = _foreground_hwnd()
            return (ok if after != before else bad,
                    "Switched windows." if after != before else "The foreground window did not change.",
                    {"before": before, "after": after})
        if action in _POWER:
            _run_power(action)
            return ok, f"Okay, {action} started.", {"action": action}
        raise SkillValidationError(f"unsupported windows_system action '{action}'")
