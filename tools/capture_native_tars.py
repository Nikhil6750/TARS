import ctypes
import os
import time
from ctypes import wintypes
from PIL import ImageGrab, Image

user32 = ctypes.windll.user32
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

ARTIFACT_DIR = r"C:\Users\nikhi\.gemini\antigravity-ide\brain\2097f650-72f8-4c06-a23b-194b0c55491e"
os.makedirs(ARTIFACT_DIR, exist_ok=True)

def find_tars_window():
    import psutil
    found = []

    def enum_proc(hwnd, lparam):
        if not user32.IsWindow(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            p = psutil.Process(pid.value)
            if "tars-companion" in p.name().lower():
                length = user32.GetWindowTextLengthW(hwnd)
                buff = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buff, length + 1)
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                # main window
                if w > 200 and h > 200:
                    found.append((hwnd, pid.value, buff.value, (rect.left, rect.top, rect.right, rect.bottom)))
        except Exception:
            pass
        return True

    cb = WNDENUMPROC(enum_proc)
    user32.EnumWindows(cb, 0)
    return found

windows = find_tars_window()
print(f"Found TARS windows: {windows}")

if not windows:
    # Try finding any window with TARS title
    def enum_any(hwnd, lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        buff = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buff, length + 1)
        if "TARS" in buff.value:
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            windows.append((hwnd, 0, buff.value, (rect.left, rect.top, rect.right, rect.bottom)))
        return True
    cb2 = WNDENUMPROC(enum_any)
    user32.EnumWindows(cb2, 0)
    print(f"Fallback search found: {windows}")

if windows:
    hwnd = windows[0][0]
    # Bring to foreground
    SW_RESTORE = 9
    SW_SHOW = 5
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.ShowWindow(hwnd, SW_SHOW)
    user32.SetForegroundWindow(hwnd)
    time.sleep(1.0)

    # Capture 1: Default launch view
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    bbox = (rect.left, rect.top, rect.right, rect.bottom)
    print(f"Capturing native window at {bbox}")
    img_default = ImageGrab.grab(bbox=bbox)
    default_path = os.path.join(ARTIFACT_DIR, "native_tars_default_assistant.png")
    img_default.save(default_path)
    print(f"Saved default view to {default_path}")

    # Resize checks: 1366x768, 1536x864
    SWP_NOMOVE = 0x0002
    SWP_NOZORDER = 0x0004
    
    # 1366x768
    user32.SetWindowPos(hwnd, 0, 50, 50, 1366, 768, SWP_NOZORDER)
    time.sleep(0.8)
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    bbox_1366 = (rect.left, rect.top, rect.right, rect.bottom)
    img_1366 = ImageGrab.grab(bbox=bbox_1366)
    path_1366 = os.path.join(ARTIFACT_DIR, "native_tars_1366x768.png")
    img_1366.save(path_1366)
    print(f"Saved 1366x768 to {path_1366}")

    # 1536x864 (or 1440x840)
    user32.SetWindowPos(hwnd, 0, 30, 30, 1440, 840, SWP_NOZORDER)
    time.sleep(0.8)
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    bbox_1536 = (rect.left, rect.top, rect.right, rect.bottom)
    img_1536 = ImageGrab.grab(bbox=bbox_1536)
    path_1536 = os.path.join(ARTIFACT_DIR, "native_tars_1536x864.png")
    img_1536.save(path_1536)
    print(f"Saved 1536x864 to {path_1536}")

else:
    print("No TARS window found to capture.")
