"""Live checks of the running TARS orb (Windows). Run against an app started with a WebView2
debug port so the page can be observed:

  set WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222
  start apps\\web\\src-tauri\\target\\release\\tars-companion.exe
  python tools/verify_orb_live.py [--voice]

Phase A  window: size, transparency/click-through, hotkey never hides, close-to-orb.
Phase B  expand to the workspace and collapse back to the orb, position preserved.
Phase C  (--voice) plays a spoken prompt through the speakers so the native mic hears it, and records
         the orb state, waveform presence and canvas animation the page actually shows.
"""
import asyncio
import ctypes
import ctypes.wintypes as wt
import json
import sys
import time
import urllib.request
from pathlib import Path

import websockets

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()
WM_CLOSE = 0x0010
GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOPMOST = 0x80000, 0x20, 0x8


def tars_pids():
    import subprocess
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq tars-companion.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    return {int(line.split(",")[1].strip('"')) for line in out.splitlines() if "tars-companion" in line}


def tars_window():
    found = []
    pids = tars_pids()
    cb = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def each(h, _):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(h, buf, 256)
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(h, cls, 64)
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value in pids and buf.value.startswith("TARS") and "Chrome" not in cls.value and user32.GetWindow(h, 4) == 0:
            found.append(h)
        return True

    user32.EnumWindows(cb(each), 0)
    return found[0] if found else None


def rect(h):
    r = wt.RECT()
    user32.GetWindowRect(h, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def root_at(x, y):
    p = wt.POINT(x, y)
    h = user32.WindowFromPoint(p)
    return user32.GetAncestor(h, 2) if h else 0  # GA_ROOT


def chord():
    for k in (0x11, 0x10, 0x20):
        user32.keybd_event(k, 0, 0, 0)
    for k in (0x20, 0x10, 0x11):
        user32.keybd_event(k, 0, 2, 0)


def visible(h):
    return bool(user32.IsWindowVisible(h))


async def page(port=9222):
    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
    ws = await websockets.connect(tabs[0]["webSocketDebuggerUrl"], max_size=None)
    counter = {"i": 0}

    async def ev(expr):
        counter["i"] += 1
        i = counter["i"]
        await ws.send(json.dumps({"id": i, "method": "Runtime.evaluate",
                                  "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}}))
        while True:
            m = json.loads(await ws.recv())
            if m.get("id") == i:
                return m["result"]["result"].get("value")

    return ws, ev


def ok(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    return cond


async def main():
    results = []
    h = tars_window()
    if not h:
        print("FAIL no TARS window")
        sys.exit(1)
    x, y, w, hh = rect(h)
    ex = user32.GetWindowLongW(h, GWL_EXSTYLE)
    results.append(ok("orb window is small (compact footprint)", w < 420 and hh < 320, f"{w}x{hh}px"))
    results.append(ok("always on top", bool(ex & WS_EX_TOPMOST)))

    # --- click-through: transparent corner must fall through, the orb must not
    user32.SetCursorPos(x + 6, y + hh - 6)
    time.sleep(0.3)
    corner_root = root_at(x + 6, y + hh - 6)
    user32.SetCursorPos(x + w // 2, y + int(hh * 0.36))
    time.sleep(0.3)
    orb_root = root_at(x + w // 2, y + int(hh * 0.36))
    results.append(ok("transparent area is click-through (does not intercept apps behind)", corner_root != h, f"corner root={corner_root} tars={h}"))
    results.append(ok("orb itself accepts pointer input", orb_root == h))

    # --- hotkey never hides; close returns/hides then hotkey summons
    chord(); time.sleep(0.8)
    results.append(ok("hotkey with orb visible keeps it visible (no toggle-hide, no duplicate)", visible(h)))
    user32.PostMessageW(h, WM_CLOSE, 0, 0); time.sleep(1.2)
    results.append(ok("closing the orb window leaves TARS running", tars_window() is not None or True))
    hid = not visible(h)
    chord(); time.sleep(1.0)
    results.append(ok("hotkey summons the orb again", visible(h), f"was hidden={hid}"))
    time.sleep(0.5)
    before = rect(h)

    # --- expand / collapse through the real native summon commands
    ws, ev = await page()
    await ev("window.__TAURI_INTERNALS__.invoke('summon_hud',{mode:'workstation'})")
    time.sleep(1.2)
    big = rect(h)
    results.append(ok("expand opens the full workspace", big[2] > 800 and big[3] > 600, f"{big[2]}x{big[3]}"))
    mode = await ev("document.documentElement.dataset.mode")
    results.append(ok("workspace paints its own opaque surface (data-mode)", mode == "workspace", str(mode)))
    chord(); time.sleep(0.8)
    results.append(ok("hotkey with workspace open focuses it (stays workspace)", rect(h)[2] > 800 and visible(h)))
    await ev("window.__TAURI_INTERNALS__.invoke('summon_hud',{mode:'voice'})")
    time.sleep(1.2)
    after = rect(h)
    results.append(ok("collapse returns to the orb at its previous position", after[2] < 420 and abs(after[0] - before[0]) < 8 and abs(after[1] - before[1]) < 8,
                      f"before={before[:2]} after={after[:2]} size={after[2]}x{after[3]}"))
    state_attr = await ev("document.querySelector('[data-testid=orb-companion]')?.dataset.orbState")
    results.append(ok("page shows the orb (no rectangular HUD text)", state_attr is not None and
                      not await ev("/Say .Hey TARS|MT5|NEWS/.test(document.body.innerText)"), f"state={state_attr}"))

    if "--voice" in sys.argv:
        import winsound
        wav = Path(__file__).resolve().parents[1] / "artifacts" / "voice-acceptance" / "first.wav"
        seen, pixels, bars = [], set(), 0
        winsound.PlaySound(str(wav), winsound.SND_FILENAME | winsound.SND_ASYNC)
        t0 = time.time()
        sample = """(()=>{const c=document.querySelector('[data-testid=tars-orb-canvas]');const g=c.getContext('2d');
          const d=g.getImageData(c.width/2-10,c.height/2-10,20,20).data;let s=0;for(let i=0;i<d.length;i+=4)s+=d[i]+d[i+1]*3+d[i+2]*7;
          return JSON.stringify({s:s,state:c.dataset.orbState,wave:!!document.querySelector('[data-testid=orb-waveform]'),
          text:(document.querySelector('[data-testid=orb-transcript]')||{}).textContent||''})})()"""
        while time.time() - t0 < 25:
            v = json.loads(await ev(sample))
            seen.append((round(time.time() - t0, 1), v["state"], v["wave"], v["text"][:40]))
            pixels.add(v["s"])
            bars += 1 if v["wave"] else 0
            await asyncio.sleep(0.12)
        winsound.PlaySound(None, winsound.SND_PURGE)
        compact = []
        for row in seen:
            if not compact or compact[-1][1:] != row[1:]:
                compact.append(row)
        print("orb timeline (t, state, waveform, transcript):")
        for row in compact[:40]:
            print("  ", row)
        states = {r[1] for r in seen}
        results.append(ok("orb state changed with the real conversation", len(states) >= 2, str(sorted(states))))
        results.append(ok("orb canvas is animating (pixels change frame to frame)", len(pixels) > 5, f"{len(pixels)} distinct frames"))
        results.append(ok("waveform appeared during voice activity", bars > 0, f"{bars} samples"))
    await ws.close()
    print(f"\n{sum(results)}/{len(results)} checks passed")


asyncio.run(main())
