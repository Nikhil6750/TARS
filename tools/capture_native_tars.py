"""Capture and verify the actual Windows TARS/Tauri window.

This intentionally targets the native ``tars-companion.exe`` process. It
does not accept a Vite/browser page as native evidence. Window pixels are
captured with ``PrintWindow`` so an occluded window is still captured.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import tempfile
import time
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class NativeWindow:
    hwnd: int
    pid: int
    title: str
    left: int
    top: int
    right: int
    bottom: int
    visible: bool

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


def choose_main_window(windows: list[NativeWindow]) -> NativeWindow | None:
    """Choose the largest visible, titled application window."""

    eligible = [
        item
        for item in windows
        if item.visible and item.width >= 100 and item.height >= 100 and item.title
    ]
    return max(eligible, key=lambda item: item.width * item.height, default=None)


def enumerate_windows(pid: int) -> list[NativeWindow]:
    if os.name != "nt":
        raise RuntimeError("native TARS capture is Windows-only")
    user32 = ctypes.windll.user32
    user32.SetProcessDPIAware()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found: list[NativeWindow] = []

    @callback_type
    def callback(hwnd: int, _lparam: int) -> bool:
        owner_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        if owner_pid.value != pid:
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        found.append(
            NativeWindow(
                hwnd=int(hwnd),
                pid=pid,
                title=title.value,
                left=rect.left,
                top=rect.top,
                right=rect.right,
                bottom=rect.bottom,
                visible=bool(user32.IsWindowVisible(hwnd)),
            )
        )
        return True

    user32.EnumWindows(callback, 0)
    return found


def find_process_id(explicit_pid: int | None, process_name: str) -> int:
    import psutil

    if explicit_pid is not None:
        process = psutil.Process(explicit_pid)
        if process.name().lower() != process_name.lower():
            raise RuntimeError(
                f"PID {explicit_pid} is {process.name()!r}, not {process_name!r}"
            )
        return explicit_pid
    matches = [
        process
        for process in psutil.process_iter(["pid", "name", "create_time"])
        if (process.info["name"] or "").lower() == process_name.lower()
    ]
    if not matches:
        raise RuntimeError(f"no running {process_name} process found")
    return max(matches, key=lambda process: process.info["create_time"] or 0).pid


def capture_window(hwnd: int, destination: Path) -> None:
    try:
        import win32gui
        import win32ui
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("capture requires Pillow and pywin32") from exc

    user32 = ctypes.windll.user32
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    window_dc = win32gui.GetWindowDC(hwnd)
    source_dc = win32ui.CreateDCFromHandle(window_dc)
    memory_dc = source_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(source_dc, width, height)
    memory_dc.SelectObject(bitmap)
    try:
        result = user32.PrintWindow(hwnd, memory_dc.GetSafeHdc(), 2)
        if result != 1:
            raise RuntimeError(f"PrintWindow failed for HWND 0x{hwnd:x}")
        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        image = Image.frombuffer(
            "RGB",
            (info["bmWidth"], info["bmHeight"]),
            bits,
            "raw",
            "BGRX",
            0,
            1,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination)
    finally:
        import win32gui

        win32gui.DeleteObject(bitmap.GetHandle())
        memory_dc.DeleteDC()
        source_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, window_dc)


def verify_navigation(hwnd: int, output_dir: Path) -> list[dict[str, object]]:
    try:
        import uiautomation as auto
    except ImportError as exc:
        raise RuntimeError("navigation verification requires uiautomation") from exc

    root = auto.ControlFromHandle(hwnd)
    results: list[dict[str, object]] = []
    for tab in ("Chat", "Workspace", "Memory", "Settings"):
        control = root.ButtonControl(searchDepth=30, Name=tab)
        exists = control.Exists(2, 0.2)
        if exists:
            control.Click()
            time.sleep(0.8)
            capture_window(hwnd, output_dir / f"native-{tab.lower()}.png")
        results.append({"tab": tab, "found": exists, "clicked": exists})
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--process-name", default="tars-companion.exe")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "tars-native-evidence",
    )
    parser.add_argument("--verify-navigation", action="store_true")
    parser.add_argument("--minimum-width", type=int, default=800)
    parser.add_argument("--minimum-height", type=int, default=500)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload: dict[str, object] = {"native": True, "errors": []}
    try:
        pid = find_process_id(args.pid, args.process_name)
        windows = enumerate_windows(pid)
        main_window = choose_main_window(windows)
        payload["pid"] = pid
        payload["windows"] = [
            asdict(item) | {"width": item.width, "height": item.height}
            for item in windows
        ]
        if main_window is None:
            raise RuntimeError("native process exists but has no usable visible main window")
        payload["main_window"] = asdict(main_window) | {
            "width": main_window.width,
            "height": main_window.height,
        }
        capture_window(main_window.hwnd, args.output_dir / "native-launch.png")
        if main_window.width < args.minimum_width or main_window.height < args.minimum_height:
            payload["errors"].append(
                "main window is compact/clipped: "
                f"{main_window.width}x{main_window.height}, expected at least "
                f"{args.minimum_width}x{args.minimum_height}"
            )
        if args.verify_navigation:
            navigation = verify_navigation(main_window.hwnd, args.output_dir)
            payload["navigation"] = navigation
            missing = [item["tab"] for item in navigation if not item["found"]]
            if missing:
                payload["errors"].append(f"missing native navigation controls: {missing}")
        payload["output_dir"] = str(args.output_dir.resolve())
    except Exception as exc:  # noqa: BLE001 - diagnostics return structured evidence
        payload["errors"].append(str(exc))
    print(json.dumps(payload, indent=2))
    raise SystemExit(1 if payload["errors"] else 0)


if __name__ == "__main__":
    main()
