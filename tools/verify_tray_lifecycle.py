"""Live Windows lifecycle check of the running native app (needs start_tars.ps1 running).

Verifies with real Win32 calls: main window visible -> WM_CLOSE leaves the process alive
with the window hidden (close-to-tray) -> global hotkey Ctrl+Shift+Space brings it back
-> the same hotkey toggles it hidden again. Tray icon click/menu and Quit are NOT covered
(no reliable programmatic access to the notification area); see the demo checklist.

python tools/verify_tray_lifecycle.py
"""
import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
import time

user32 = ctypes.windll.user32
WM_CLOSE = 0x0010
KEYEVENTF_KEYUP = 0x0002
VK_CTRL, VK_SHIFT, VK_SPACE = 0x11, 0x10, 0x20


def tars_pids():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq tars-companion.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True).stdout
    return {int(line.split(",")[1].strip('"')) for line in out.splitlines() if "tars-companion" in line}


def windows_of(pids):
    found = []
    cb = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def each(hwnd, _):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if buf.value.startswith("TARS"):
                found.append(hwnd)
        return True

    user32.EnumWindows(cb(each), 0)
    return found


def chord():
    for k in (VK_CTRL, VK_SHIFT, VK_SPACE):
        user32.keybd_event(k, 0, 0, 0)
    for k in (VK_SPACE, VK_SHIFT, VK_CTRL):
        user32.keybd_event(k, 0, KEYEVENTF_KEYUP, 0)


def main():
    pids = tars_pids()
    if not pids:
        print("FAIL: tars-companion.exe is not running")
        sys.exit(1)
    hwnds = windows_of(pids)
    if not hwnds:
        print("FAIL: no TARS window found")
        sys.exit(1)
    h = hwnds[0]
    vis = lambda: bool(user32.IsWindowVisible(h))  # noqa: E731
    results = {}
    results["visible_at_start"] = vis()
    user32.PostMessageW(h, WM_CLOSE, 0, 0)
    time.sleep(2)
    results["close_to_tray: process alive"] = bool(tars_pids())
    results["close_to_tray: window hidden"] = not vis()
    chord()
    time.sleep(2)
    results["hotkey reopens window"] = vis()
    chord()
    time.sleep(2)
    results["hotkey toggles hidden"] = not vis()
    chord()
    time.sleep(1.5)
    for k, v in results.items():
        print(("PASS " if v else "FAIL ") + k)
    sys.exit(0 if all(results.values()) else 1)


main()
