"""Desktop verification for the TARS Market Intelligence window.

This checks the *native* application, not a browser page. It targets the real
`tars-companion.exe` process, finds its Market Intelligence window by title,
captures its pixels with PrintWindow, drives fullscreen through a real F11
keystroke, and confirms the Python backend is supervised as a single child
that leaves no orphan behind.

Window enumeration and capture are reused from `capture_native_tars.py`
rather than reimplemented.

Usage:
    python tools/verify_market_desktop.py                 # verify a running app
    python tools/verify_market_desktop.py --quit-after    # also verify shutdown
"""
from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import time
import urllib.request
from ctypes import wintypes
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import psutil
from capture_native_tars import (
    NativeWindow,
    capture_window,
    enumerate_windows,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT_DIR = Path("artifacts/market-desktop")
PROCESS_NAME = "tars-companion.exe"
MARKET_TITLE_FRAGMENT = "Market Intelligence"
BACKEND_PORT = 8000
HEALTH_URL = f"http://127.0.0.1:{BACKEND_PORT}/api/v1/market/health"
# Distinct colours separating a painted dashboard from the error panel.
# Observed: ~450 for "cannot reach the backend", ~3000 once painted.
DASHBOARD_MIN_COLOURS = 1500

user32 = ctypes.WinDLL("user32", use_last_error=True)

# Without this Windows virtualises every coordinate for a scaled display, so
# the same window measures differently depending on what ran first, and a
# 1920x1080 monitor at 125% reads as 1536x864. Must run before any measurement.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
except OSError:
    pass

VK_F11 = 0x7A
KEYEVENTF_KEYUP = 0x0002
VK_MENU = 0x12
WM_CLOSE = 0x0010
SW_RESTORE = 9


class Report:
    """PASS, FAIL (the application misbehaved) and UNVERIFIED (this harness
    could not perform the check, which says nothing about the application)."""

    def __init__(self) -> None:
        self.results: list[tuple[str, bool | None, str]] = []

    def check(self, name: str, ok: bool | None, evidence: str) -> bool:
        self.results.append((name, ok, evidence))
        verdict = "UNVERIFIED" if ok is None else ("PASS" if ok else "FAIL")
        print(f"  [{verdict}] {name}\n         {evidence}")
        return bool(ok)

    @property
    def failed(self) -> list[str]:
        return [n for n, ok, _ in self.results if ok is False]

    @property
    def unverified(self) -> list[str]:
        return [n for n, ok, _ in self.results if ok is None]

    def summary(self) -> str:
        passed = sum(1 for _, ok, _ in self.results if ok)
        return (
            f"{passed}/{len(self.results)} desktop checks passed, {len(self.failed)} failed, "
            f"{len(self.unverified)} could not be verified"
        )


# ---- native helpers ------------------------------------------------------
def shell_processes() -> list[psutil.Process]:
    out = []
    for proc in psutil.process_iter(["name", "pid", "create_time"]):
        if (proc.info["name"] or "").lower() == PROCESS_NAME:
            out.append(proc)
    return out


def backend_processes() -> list[psutil.Process]:
    """Every Python process that looks like this repo's backend, whichever
    port it serves. Used only for reporting context."""
    out = []
    for proc in psutil.process_iter(["name", "pid", "cmdline"]):
        name = (proc.info["name"] or "").lower()
        if not name.startswith("python"):
            continue
        cmd = " ".join(proc.info["cmdline"] or []).lower()
        if "run.py" in cmd or ("uvicorn" in cmd and "app.main" in cmd):
            out.append(proc)
    return out


def supervised_backends(shell_pid: int, port: int = BACKEND_PORT) -> list[psutil.Process]:
    """The backend(s) belonging to this desktop app: serving the supervised
    port, or descended from the shell process.

    Scoped deliberately. A developer may have unrelated backends running on
    other ports (this repo's own verification starts some), and counting
    those would turn an unrelated process into a false 'duplicate backend'
    failure. Duplication is only meaningful for the supervised instance.
    """
    listeners = listener_pids(port)
    out: list[psutil.Process] = []
    for proc in backend_processes():
        try:
            if proc.pid in listeners:
                out.append(proc)
                continue
            parent = proc.parent()
            if parent is not None and parent.pid == shell_pid:
                out.append(proc)
        except psutil.Error:
            continue
    return out


def listener_pids(port: int) -> set[int]:
    pids = set()
    for conn in psutil.net_connections(kind="inet"):
        if (conn.status == psutil.CONN_LISTEN and conn.laddr
                and conn.laddr.port == port and conn.pid):
            pids.add(conn.pid)
    return pids


def market_window(pid: int) -> NativeWindow | None:
    for win in enumerate_windows(pid):
        if MARKET_TITLE_FRAGMENT.lower() in win.title.lower():
            return win
    return None


def refresh(pid: int, hwnd: int) -> NativeWindow | None:
    for win in enumerate_windows(pid):
        if win.hwnd == hwnd:
            return win
    return None


def is_minimized(hwnd: int) -> bool:
    return bool(user32.IsIconic(hwnd))


def focus(hwnd: int) -> bool:
    """Bring the window forward, and say whether that actually worked. Windows
    refuses the foreground to a background process unless it has just sent
    input, hence the Alt tap, and a refused request is silent."""
    user32.ShowWindow(hwnd, SW_RESTORE)
    for _ in range(3):
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.6)
        if user32.GetForegroundWindow() == hwnd:
            return True
    return False


def press_f11() -> None:
    user32.keybd_event(VK_F11, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_F11, 0, KEYEVENTF_KEYUP, 0)


def monitor_size_for(hwnd: int) -> tuple[int, int]:
    """Size of the monitor the window is on, so a fullscreen check compares
    against the right display on a multi-monitor desktop."""
    MONITOR_DEFAULTTONEAREST = 2

    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                    ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]

    handle = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(handle, ctypes.byref(info))
    rect = info.rcMonitor
    return rect.right - rect.left, rect.bottom - rect.top


def image_content_level(path: Path) -> tuple[bool, str, int]:
    """How much distinct content a captured window holds.

    Measured as distinct colours in a downsampled frame. The numbers are far
    apart in practice, which is what makes this a usable signal: the
    backend-unreachable panel is a few hundred dark tones, a painted
    dashboard — eighteen glowing curves, candles, tables — is thousands.
    """
    try:
        from PIL import Image
    except ImportError:
        return True, "Pillow unavailable; existence-only check", DASHBOARD_MIN_COLOURS
    with Image.open(path) as img:
        small = img.convert("RGB").resize((160, 100))
        colours = {small.getpixel((x, y)) for x in range(160) for y in range(100)}
    n = len(colours)
    summary = (
        f"{n} distinct colours in a 160x100 sample "
        f"(>= {DASHBOARD_MIN_COLOURS} means a painted dashboard, not an error panel)"
    )
    return n >= DASHBOARD_MIN_COLOURS, summary, n


def api(url: str, timeout: float = 20.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read())


# ---- verification --------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quit-after", action="store_true",
                    help="close the app at the end and verify no orphan backend survives")
    ap.add_argument("--timeout", type=float, default=240.0,
                    help="seconds to wait for the desktop window to appear")
    ap.add_argument("--launch", type=Path, metavar="EXE",
                    help="start this binary first, unless TARS is already running")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report = Report()
    print("==== TARS Market Intelligence — desktop verification ====\n")

    if args.launch and not shell_processes():
        subprocess.Popen([str(args.launch), "--market"], close_fds=True)

    # 1. the native shell is running, exactly once
    deadline = time.time() + args.timeout
    shells: list[psutil.Process] = []
    while time.time() < deadline:
        shells = shell_processes()
        if shells:
            break
        time.sleep(2)
    if not shells:
        # This script does not start the app unless told to, so an absent
        # process is a setup problem and not an application failure.
        report.check("Native desktop shell is running", None,
                     f"no {PROCESS_NAME} process after {args.timeout:.0f}s; "
                     "start it first, or pass --launch EXE")
        print("\n==== " + report.summary() + " ====")
        return 2

    report.check(
        "Native desktop shell is running as a single instance",
        len(shells) == 1,
        f"{len(shells)} x {PROCESS_NAME} (pid {[p.pid for p in shells]})",
    )
    shell_pid = shells[0].pid

    # 2. the Market Intelligence window exists as its own top-level window
    win = None
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        win = market_window(shell_pid)
        if win and win.visible and (win.width > 200 or is_minimized(win.hwnd)):
            break
        time.sleep(2)

    if not win:
        titles = [w.title for w in enumerate_windows(shell_pid)]
        report.check("Market Intelligence window exists", False,
                     f"no window titled '{MARKET_TITLE_FRAGMENT}'; saw {titles}")
        print("\n==== " + report.summary() + " ====")
        return 1

    # A minimized window sits at -32000,-32000 at 199x34 and cannot be captured
    # or sent keys, so it would otherwise surface as several unrelated failures.
    # Named once here, then restored so the remaining checks measure something.
    came_up_minimized = is_minimized(win.hwnd)
    report.check(
        "The Market window did not open minimized",
        not came_up_minimized,
        f"at {win.left},{win.top} size {win.width}x{win.height}"
        + ("; restoring it so the remaining checks can run" if came_up_minimized else ""),
    )
    if came_up_minimized:
        user32.ShowWindow(win.hwnd, SW_RESTORE)
        time.sleep(1.5)
        win = refresh(shell_pid, win.hwnd) or win

    all_windows = enumerate_windows(shell_pid)
    report.check(
        "Market Intelligence opens as its own native desktop window",
        MARKET_TITLE_FRAGMENT.lower() in win.title.lower() and win.visible,
        f"title={win.title!r} hwnd={win.hwnd} at {win.left},{win.top} "
        f"size {win.width}x{win.height}; process has {len(all_windows)} window(s): "
        f"{[w.title for w in all_windows]}",
    )
    report.check(
        "It is a real desktop-sized window, not a compact popup",
        win.width >= 1000 and win.height >= 650,
        f"{win.width}x{win.height} (minimum enforced by the builder: 1024x680)",
    )

    # 3. capture real pixels
    # 3. capture real pixels, once the dashboard has actually painted.
    #
    # On a cold start the window opens while the supervised backend is still
    # coming up, so the first paint is the "cannot reach the backend" panel.
    # That panel is a handful of dark colours; a populated dashboard has
    # thousands. Polling for the richer image is therefore a direct test of
    # the app's automatic retry, not just a screenshot.
    shot = OUT_DIR / "01-market-window.png"
    colours = 0
    detail = ""
    deadline = time.time() + 120
    while time.time() < deadline:
        capture_window(win.hwnd, shot)
        populated, detail, colours = image_content_level(shot)
        if populated:
            break
        time.sleep(4)
    report.check(
        "Dashboard paints a populated view (recovers if the backend was still starting)",
        colours >= DASHBOARD_MIN_COLOURS,
        f"{shot} — {detail}",
    )

    # 4. backend supervision: one supervised backend, owned by the shell
    backends = supervised_backends(shell_pid)
    listeners = listener_pids(BACKEND_PORT)
    others = [p.pid for p in backend_processes() if p not in backends]
    report.check(
        "Exactly one supervised Python backend, with no duplicates",
        len(backends) == 1,
        f"{len(backends)} supervised backend(s): {[p.pid for p in backends]}"
        + (f"; unrelated backends on other ports: {others}" if others else ""),
    )
    report.check(
        f"Exactly one listener on the supervised port {BACKEND_PORT}",
        len(listeners) == 1,
        f"pids listening on {BACKEND_PORT}: {sorted(listeners)}",
    )
    if backends:
        parent = backends[0].parent()
        report.check(
            "The backend is a child of the desktop shell (supervised lifecycle)",
            parent is not None and parent.pid == shell_pid,
            f"backend pid {backends[0].pid} parent = "
            f"{parent.pid if parent else None} ({parent.name() if parent else 'n/a'}), "
            f"shell pid = {shell_pid}",
        )

    # 5. the dashboard must never present recorded history as live
    try:
        health = api(HEALTH_URL)
        status = health["status"]
        report.check(
            "Backend reachable from the desktop app and reports store coverage",
            bool(health.get("ok")) and health["store"]["symbols"] > 0,
            f"{health['store']['symbols']} instruments, last bar {health['store']['last_bar']}",
        )
        report.check(
            "Historical data is marked REPLAY and cannot be mistaken for live",
            status["status"] == "REPLAY" and status["replay"] is True
            and "not a live feed" in status["detail"].lower(),
            f"status={status['status']} replay={status['replay']} :: {status['detail']}",
        )
        report.check(
            "Trading execution disabled",
            health.get("trading_execution") == "DISABLED",
            f"trading_execution={health.get('trading_execution')}",
        )
    except Exception as exc:  # noqa: BLE001
        report.check("Backend reachable from the desktop app", False, f"{type(exc).__name__}: {exc}")

    # 6. fullscreen, via a real keystroke
    delivered = focus(win.hwnd)
    mon_w, mon_h = monitor_size_for(win.hwnd)
    before = refresh(shell_pid, win.hwnd) or win
    if delivered:
        press_f11()
        time.sleep(2.0)
    full = refresh(shell_pid, win.hwnd)
    # In fullscreen the window rect is exactly the monitor. A *windowed* rect
    # is normally larger than its client area -- Windows includes the
    # invisible resize border -- so "bigger than the monitor" is not a
    # fullscreen signal; being exactly the monitor is.
    is_full = bool(full and full.width == mon_w and full.height == mon_h)
    if not delivered:
        report.check(
            "F11 puts the window into native fullscreen", None,
            "Windows refused the foreground, so the keystroke could not be delivered",
        )
    else:
        report.check(
            "F11 puts the window into native fullscreen",
            is_full,
            f"window {full.width}x{full.height}, monitor {mon_w}x{mon_h}"
            if full else "window vanished",
        )
    if is_full:
        capture_window(win.hwnd, OUT_DIR / "02-market-fullscreen.png")

    if delivered:
        press_f11()
        time.sleep(2.0)
    restored = refresh(shell_pid, win.hwnd)
    if not delivered:
        report.check("F11 again restores the window to its previous size", None,
                     "not attempted: fullscreen was never entered")
    else:
        report.check(
            "F11 again restores the window to its previous size",
            bool(restored and restored.width == before.width and restored.height == before.height),
            f"{restored.width}x{restored.height} after restore, "
            f"was {before.width}x{before.height} before fullscreen"
            if restored else "window vanished",
        )

    # 7. resizing
    if restored:
        user32.MoveWindow(win.hwnd, restored.left, restored.top, 1280, 820, True)
        time.sleep(1.2)
        resized = refresh(shell_pid, win.hwnd)
        report.check(
            "Window is resizable",
            bool(resized and abs(resized.width - 1280) <= 24),
            f"resized to {resized.width}x{resized.height} (requested 1280x820)"
            if resized else "window vanished",
        )
        capture_window(win.hwnd, OUT_DIR / "03-market-resized.png")

    # 8. closing the market window must not take the app down
    user32.PostMessageW(win.hwnd, WM_CLOSE, 0, 0)
    time.sleep(2.5)
    still_open = market_window(shell_pid)
    shell_alive = psutil.pid_exists(shell_pid)
    report.check(
        "Closing the Market window leaves the rest of TARS running",
        still_open is None and shell_alive,
        f"market window closed={still_open is None}; {PROCESS_NAME} still alive={shell_alive}; "
        f"remaining windows: {[w.title for w in enumerate_windows(shell_pid)] if shell_alive else []}",
    )
    report.check(
        "Backend survives closing the Market window",
        len(supervised_backends(shell_pid)) == 1,
        f"{len(supervised_backends(shell_pid))} supervised backend(s) still running",
    )

    # 8b. reopening: `--market` routes through the single-instance plugin to
    # the running app rather than starting a second copy.
    exe = None
    for proc in shell_processes():
        try:
            exe = proc.exe()
        except psutil.Error:
            pass
    if exe:
        subprocess.Popen([exe, "--market"], close_fds=True)
        reopened = None
        deadline = time.time() + 30
        while time.time() < deadline:
            reopened = market_window(shell_pid)
            if reopened and reopened.visible:
                break
            time.sleep(1.5)
        shells_now = shell_processes()
        report.check(
            "Relaunching with --market reopens the window without a second instance",
            reopened is not None and len(shells_now) == 1,
            f"window reopened={reopened is not None}; "
            f"{PROCESS_NAME} instances={len(shells_now)} (pids {[p.pid for p in shells_now]})",
        )
        if reopened:
            capture_window(reopened.hwnd, OUT_DIR / "04-market-reopened.png")
            win = reopened

    # 9. optional: full shutdown leaves no orphan
    if args.quit_after:
        print("\n  -- quitting the app cooperatively to verify shutdown --")
        before = [p.pid for p in supervised_backends(shell_pid)]
        # The app's own quit path (identical to the tray's Quit item): it asks
        # the backend to exit first. A hard TerminateProcess on the shell
        # would orphan the backend, which tests the OS rather than the
        # application.
        quit_exe = None
        for proc in shell_processes():
            try:
                quit_exe = proc.exe()
            except psutil.Error:
                pass
        if quit_exe:
            subprocess.Popen([quit_exe, "--quit"], close_fds=True)
        gone_deadline = time.time() + 60
        while time.time() < gone_deadline and (shell_processes() or listener_pids(BACKEND_PORT)):
            time.sleep(2)
        after = sorted(listener_pids(BACKEND_PORT))
        report.check(
            "Cooperative quit stops the desktop shell",
            not shell_processes(),
            f"{PROCESS_NAME} instances remaining: {[p.pid for p in shell_processes()]}",
        )
        report.check(
            "Quitting the desktop app leaves no orphan Python backend",
            not after,
            f"supervised backend pids before quit {before}; "
            f"still listening on {BACKEND_PORT} after quit: {after}",
        )

    payload = {
        "shell_pid": shell_pid,
        "market_window": asdict(win) | {"width": win.width, "height": win.height},
        "output_dir": str(OUT_DIR.resolve()),
        "checks": [{"name": n, "ok": ok, "evidence": e} for n, ok, e in report.results],
    }
    (OUT_DIR / "desktop-verification.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )

    print(f"\n  evidence -> {OUT_DIR.resolve()}")
    print("\n==== " + report.summary() + " ====")
    if report.failed:
        print("FAILED: " + ", ".join(report.failed))
    if report.unverified:
        print("COULD NOT VERIFY: " + ", ".join(report.unverified))
    if report.failed:
        return 1
    if report.unverified:
        return 2
    print("All desktop checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
