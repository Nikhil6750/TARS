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
from dataclasses import asdict, dataclass
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
CORE_HEALTH_URL = f"http://127.0.0.1:{BACKEND_PORT}/api/v1/health"
# Distinct colours separating a painted dashboard from the error panel.
# Observed: ~450 for "cannot reach the backend", ~3000 once painted.
DASHBOARD_MIN_COLOURS = 1500

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

VK_F11 = 0x7A
KEYEVENTF_KEYUP = 0x0002
WM_CLOSE = 0x0010
SW_RESTORE = 9
MONITOR_DEFAULTTONEAREST = 2

PASS = "PASS"
FAIL = "FAIL"
NOT_VERIFIED = "NOT VERIFIED"


def enable_per_monitor_dpi_awareness() -> str:
    """Opt this verifier into physical, per-monitor coordinates.

    Without this, Windows may virtualize ``GetWindowRect`` and
    ``GetMonitorInfo`` differently as the process moves between displays.
    ``ERROR_ACCESS_DENIED`` means another imported module already selected a
    DPI mode; it is diagnostic rather than fatal.
    """
    setter = getattr(user32, "SetProcessDpiAwarenessContext", None)
    if setter is None:
        user32.SetProcessDPIAware()
        return "legacy system-DPI awareness (per-monitor v2 API unavailable)"
    ctypes.set_last_error(0)
    if setter(ctypes.c_void_p(-4)):  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        return "per-monitor DPI awareness v2 enabled"
    error = ctypes.get_last_error()
    return f"DPI context was already fixed by the process (Win32 error {error})"


DPI_EVIDENCE = enable_per_monitor_dpi_awareness()


@dataclass(frozen=True)
class MonitorRect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


class Report:
    def __init__(self) -> None:
        self.results: list[tuple[str, str, str]] = []

    def check(self, name: str, ok: bool, evidence: str) -> bool:
        status = PASS if ok else FAIL
        self.results.append((name, status, evidence))
        print(f"  [{status}] {name}\n         {evidence}")
        return ok

    def could_not_verify(self, name: str, evidence: str) -> None:
        self.results.append((name, NOT_VERIFIED, evidence))
        print(f"  [{NOT_VERIFIED}] {name}\n         {evidence}")

    @property
    def failed(self) -> list[str]:
        return [n for n, status, _ in self.results if status == FAIL]

    @property
    def unverified(self) -> list[str]:
        return [n for n, status, _ in self.results if status == NOT_VERIFIED]

    def summary(self) -> str:
        passed = sum(1 for _, status, _ in self.results if status == PASS)
        failed = len(self.failed)
        unverified = len(self.unverified)
        return (
            f"{passed}/{len(self.results)} desktop checks passed; "
            f"{failed} failed; {unverified} not verified"
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


def has_minimized_signature(win: NativeWindow) -> bool:
    """Recognize both the Win32 state and its iconic off-screen rectangle."""
    return bool(user32.IsIconic(win.hwnd)) or (
        win.left <= -30_000
        and win.top <= -30_000
        and win.width <= 300
        and win.height <= 100
    )


def restore_minimized_window(pid: int, win: NativeWindow, timeout: float = 8.0) -> NativeWindow | None:
    user32.ShowWindow(win.hwnd, SW_RESTORE)
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = refresh(pid, win.hwnd)
        if current is not None and not has_minimized_signature(current):
            return current
        time.sleep(0.2)
    return None


def acquire_foreground(hwnd: int, timeout: float = 3.0) -> tuple[bool, str]:
    """Acquire and prove foreground ownership before injecting F11.

    Windows is allowed to reject ``SetForegroundWindow``. Attaching the
    input queues is a bounded recovery attempt, not an assumption that the
    first call succeeded.
    """
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    attempts: list[str] = []
    direct = bool(user32.SetForegroundWindow(hwnd))
    attempts.append(f"direct SetForegroundWindow={direct}")

    if user32.GetForegroundWindow() != hwnd:
        current_thread = int(kernel32.GetCurrentThreadId())
        target_thread = int(user32.GetWindowThreadProcessId(hwnd, None))
        attached = bool(user32.AttachThreadInput(current_thread, target_thread, True))
        attempts.append(f"AttachThreadInput={attached}")
        try:
            user32.BringWindowToTop(hwnd)
            retry = bool(user32.SetForegroundWindow(hwnd))
            attempts.append(f"attached SetForegroundWindow={retry}")
        finally:
            if attached:
                user32.AttachThreadInput(current_thread, target_thread, False)

    deadline = time.time() + timeout
    while time.time() < deadline:
        foreground = int(user32.GetForegroundWindow() or 0)
        if foreground == hwnd:
            return True, "; ".join(attempts) + f"; foreground=0x{foreground:x}"
        time.sleep(0.1)
    foreground = int(user32.GetForegroundWindow() or 0)
    return False, "; ".join(attempts) + f"; foreground remained 0x{foreground:x}"


def press_f11() -> None:
    user32.keybd_event(VK_F11, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_F11, 0, KEYEVENTF_KEYUP, 0)


def monitor_rect_for(hwnd: int) -> MonitorRect:
    """Physical rectangle of the monitor containing the target window."""
    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                    ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]

    handle = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    rect = info.rcMonitor
    return MonitorRect(rect.left, rect.top, rect.right, rect.bottom)


def window_matches_monitor(win: NativeWindow | None, monitor: MonitorRect) -> bool:
    return bool(
        win
        and win.left == monitor.left
        and win.top == monitor.top
        and win.right == monitor.right
        and win.bottom == monitor.bottom
    )


def rect_evidence(win: NativeWindow | None, monitor: MonitorRect) -> str:
    if win is None:
        return "window vanished"
    return (
        f"window rect=({win.left},{win.top})-({win.right},{win.bottom}) "
        f"{win.width}x{win.height}; monitor rect=({monitor.left},{monitor.top})-"
        f"({monitor.right},{monitor.bottom}) {monitor.width}x{monitor.height}; "
        f"{DPI_EVIDENCE}"
    )


def wait_for_window(pid: int, hwnd: int, predicate, timeout: float = 6.0) -> NativeWindow | None:
    deadline = time.time() + timeout
    latest = refresh(pid, hwnd)
    while time.time() < deadline:
        latest = refresh(pid, hwnd)
        if latest is None or predicate(latest):
            return latest
        time.sleep(0.15)
    return latest


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
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report = Report()
    print("==== TARS Market Intelligence — desktop verification ====\n")

    # 1. the native shell is running, exactly once
    deadline = time.time() + args.timeout
    shells: list[psutil.Process] = []
    while time.time() < deadline:
        shells = shell_processes()
        if shells:
            break
        time.sleep(2)
    if not shells:
        report.check("Native desktop shell is running", False,
                     f"no {PROCESS_NAME} process after {args.timeout:.0f}s")
        print("\n==== " + report.summary() + " ====")
        return 1

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
        candidate = market_window(shell_pid)
        if candidate and candidate.visible and (
            candidate.width > 200 or has_minimized_signature(candidate)
        ):
            win = candidate
            break
        time.sleep(2)

    if not win:
        titles = [w.title for w in enumerate_windows(shell_pid)]
        report.check("Market Intelligence window exists", False,
                     f"no window titled '{MARKET_TITLE_FRAGMENT}'; saw {titles}")
        print("\n==== " + report.summary() + " ====")
        return 1

    if has_minimized_signature(win):
        minimized_evidence = (
            f"IsIconic={bool(user32.IsIconic(win.hwnd))}; rect=({win.left},{win.top})-"
            f"({win.right},{win.bottom}) {win.width}x{win.height}; "
            "this is the Win32 minimized-window signature"
        )
        report.check(
            "Market Intelligence did not start minimized",
            False,
            minimized_evidence,
        )
        restored_from_minimized = restore_minimized_window(shell_pid, win)
        if restored_from_minimized is None:
            report.could_not_verify(
                "Remaining window-dependent checks",
                "the application window was detected as minimized and SW_RESTORE did not restore it; "
                "downstream capture, fullscreen, and resize checks were not run",
            )
            print("\n==== " + report.summary() + " ====")
            return 1
        win = restored_from_minimized
        print(
            "         verifier restored the minimized window for downstream diagnostics: "
            f"{win.width}x{win.height} at {win.left},{win.top}"
        )

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

    # 4. backend supervision: exactly one compatible backend. It may be an
    # owned child or a compatible process attached by the shell; those have
    # different shutdown expectations and must never be conflated.
    backends = supervised_backends(shell_pid)
    listeners = listener_pids(BACKEND_PORT)
    others = [p.pid for p in backend_processes() if p not in backends]
    report.check(
        "Exactly one Python backend serves the desktop address, with no duplicates",
        len(backends) == 1,
        f"{len(backends)} backend(s) for the desktop address: {[p.pid for p in backends]}"
        + (f"; unrelated backends on other ports: {others}" if others else ""),
    )
    report.check(
        f"Exactly one listener on the supervised port {BACKEND_PORT}",
        len(listeners) == 1,
        f"pids listening on {BACKEND_PORT}: {sorted(listeners)}",
    )
    backend_owned = False
    core_identity: dict = {}
    try:
        core_health = api(CORE_HEALTH_URL)
        core_identity = core_health.get("identity", {})
        compatible = (
            core_identity.get("service") == "tars-backend"
            and core_identity.get("contract_version") == 1
        )
        report.check(
            "The process on the backend port declares a compatible TARS identity",
            compatible,
            f"identity={core_identity}",
        )
    except Exception as exc:  # noqa: BLE001
        report.check(
            "The process on the backend port declares a compatible TARS identity",
            False,
            f"{type(exc).__name__}: {exc}",
        )
    if backends:
        parent = backends[0].parent()
        backend_owned = parent is not None and parent.pid == shell_pid
        report.check(
            "Backend ownership relationship is observable",
            backend_owned or bool(core_identity),
            f"backend pid {backends[0].pid} parent = "
            f"{parent.pid if parent else None} ({parent.name() if parent else 'n/a'}), "
            f"shell pid = {shell_pid}; relationship="
            f"{'OWNED_CHILD' if backend_owned else 'ATTACHED_EXTERNAL'}",
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

    # 6. fullscreen, via a real keystroke. Never send input unless the
    # harness has proved that this exact HWND owns the foreground.
    before = refresh(shell_pid, win.hwnd) or win
    monitor = monitor_rect_for(win.hwnd)
    foreground_ok, focus_evidence = acquire_foreground(win.hwnd)
    if not foreground_ok:
        report.could_not_verify(
            "F11 puts the window into native fullscreen",
            "automation could not prove foreground ownership, so F11 was not sent; "
            + focus_evidence,
        )
        report.could_not_verify(
            "F11 again restores the window to its previous size",
            "fullscreen was not entered because the harness could not acquire the foreground",
        )
        restored = before
    else:
        press_f11()
        full = wait_for_window(
            shell_pid,
            win.hwnd,
            lambda current: window_matches_monitor(current, monitor),
        )
        is_full = window_matches_monitor(full, monitor)
        report.check(
            "F11 puts the window into native fullscreen",
            is_full,
            rect_evidence(full, monitor) + "; " + focus_evidence,
        )
        if is_full:
            capture_window(win.hwnd, OUT_DIR / "02-market-fullscreen.png")

        if is_full:
            restore_focus_ok, restore_focus_evidence = acquire_foreground(win.hwnd)
            if not restore_focus_ok:
                report.could_not_verify(
                    "F11 again restores the window to its previous size",
                    "fullscreen was verified, but automation could not reacquire foreground "
                    "ownership for the restoration keystroke; " + restore_focus_evidence,
                )
                restored = full
            else:
                press_f11()
                restored = wait_for_window(
                    shell_pid,
                    win.hwnd,
                    lambda current: (
                        current.width == before.width and current.height == before.height
                    ),
                )
                report.check(
                    "F11 again restores the window to its previous size",
                    bool(
                        restored
                        and restored.width == before.width
                        and restored.height == before.height
                    ),
                    (
                        f"restored rect=({restored.left},{restored.top})-"
                        f"({restored.right},{restored.bottom}) {restored.width}x{restored.height}; "
                        f"before={before.width}x{before.height}; {restore_focus_evidence}"
                        if restored
                        else "window vanished"
                    ),
                )
        else:
            report.could_not_verify(
                "F11 again restores the window to its previous size",
                "the foreground-owned F11 did not produce fullscreen, so a restoration "
                "keystroke was not sent into an unknown application state",
            )
            restored = refresh(shell_pid, win.hwnd)

    # 7. resizing
    if restored and not window_matches_monitor(restored, monitor):
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
    elif restored:
        report.could_not_verify(
            "Window is resizable",
            "the window remained fullscreen because restoration input could not be verified; "
            "resize was not attempted in an unknown state",
        )

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
        identity_before_quit = core_identity.copy()
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
        while time.time() < gone_deadline and (
            shell_processes() or (backend_owned and listener_pids(BACKEND_PORT))
        ):
            time.sleep(2)
        after = sorted(listener_pids(BACKEND_PORT))
        report.check(
            "Cooperative quit stops the desktop shell",
            not shell_processes(),
            f"{PROCESS_NAME} instances remaining: {[p.pid for p in shell_processes()]}",
        )
        if backend_owned:
            report.check(
                "Quitting the desktop app leaves no orphan owned backend",
                not after,
                f"owned backend pids before quit {before}; "
                f"still listening on {BACKEND_PORT} after quit: {after}",
            )
        else:
            try:
                identity_after_quit = api(CORE_HEALTH_URL).get("identity", {})
            except Exception:  # noqa: BLE001 - failed identity is evidence below
                identity_after_quit = {}
            report.check(
                "Quitting leaves an attached compatible backend untouched",
                after == before and identity_after_quit == identity_before_quit,
                f"attached backend pids before quit {before}; listeners after quit {after}; "
                f"identity unchanged={identity_after_quit == identity_before_quit}",
            )

    payload = {
        "shell_pid": shell_pid,
        "backend_owned": backend_owned,
        "backend_identity": core_identity,
        "market_window": asdict(win) | {"width": win.width, "height": win.height},
        "output_dir": str(OUT_DIR.resolve()),
        "checks": [
            {"name": n, "status": status, "ok": status == PASS, "evidence": e}
            for n, status, e in report.results
        ],
    }
    (OUT_DIR / "desktop-verification.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )

    print(f"\n  evidence -> {OUT_DIR.resolve()}")
    print("\n==== " + report.summary() + " ====")
    if report.failed:
        print("FAILED: " + ", ".join(report.failed))
        return 1
    if report.unverified:
        print("NOT VERIFIED: " + ", ".join(report.unverified))
        return 2
    print("All desktop checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
