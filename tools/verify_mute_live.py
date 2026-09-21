"""Live check of the microphone mute control with REAL mouse clicks against the running app
(start it with WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222).

Checks: the mute button click toggles only mute (no orb wake / no workspace), native mute state,
backend enforcement (frames stop reaching the voice session), unmute resumes frames.
Persistence across restart is checked by --persist (mute -> you restart the app -> run --after).
"""
import asyncio
import ctypes
import json
import sys
import time
import urllib.request

import websockets

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()
API = "http://127.0.0.1:8000/api/v1/voice/realtime/diagnostics"


def diag():
    return json.load(urllib.request.urlopen(API, timeout=3))


def ok(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    return cond


async def main():
    tabs = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json"))
    ws = await websockets.connect(next(t for t in tabs if t["type"] == "page")["webSocketDebuggerUrl"], max_size=None)
    n = {"i": 0}

    async def ev(expr):
        n["i"] += 1
        i = n["i"]
        await ws.send(json.dumps({"id": i, "method": "Runtime.evaluate", "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}}))
        while True:
            m = json.loads(await ws.recv())
            if m.get("id") == i:
                return m["result"]["result"].get("value")

    native = lambda: ev("window.__TAURI_INTERNALS__.invoke('get_mic_muted')")  # noqa: E731
    label = lambda: ev("document.querySelector('[data-testid=orb-mute]').getAttribute('aria-label')")  # noqa: E731
    orb = lambda: ev("document.querySelector('[data-testid=orb-companion]').dataset.orbState")  # noqa: E731

    if "--after" in sys.argv:
        print("native muted after restart:", await native(), "| button:", await label())
        return

    async def click_button():
        r = json.loads(await ev("(()=>{const r=document.querySelector('[data-testid=orb-mute]').getBoundingClientRect();return JSON.stringify({x:(window.screenX+r.left+r.width/2)*devicePixelRatio,y:(window.screenY+r.top+r.height/2)*devicePixelRatio})})()"))
        user32.SetCursorPos(int(r["x"]), int(r["y"]))
        time.sleep(0.35)  # click-through poller must see the cursor over the button region
        user32.mouse_event(2, 0, 0, 0, 0)
        time.sleep(0.05)
        user32.mouse_event(4, 0, 0, 0, 0)
        time.sleep(0.8)

    results = []
    start_muted = await native()
    if start_muted:
        await click_button()
    results.append(ok("starts unmuted", await native() is False and await label() == "Mute microphone"))
    f0 = diag()["mic"]["frames"]
    time.sleep(1.5)
    f1 = diag()["mic"]["frames"]
    results.append(ok("frames reach the voice session when unmuted", f1 - f0 > 30, f"+{f1 - f0} frames in 1.5 s"))

    size_before = json.loads(await ev("JSON.stringify([innerWidth,innerHeight])"))
    await click_button()
    results.append(ok("click on the mute button mutes (native authority)", await native() is True))
    results.append(ok("button shows the muted state", await label() == "Unmute microphone"))
    results.append(ok("orb did not wake, open the workspace or turn into an error", (await orb()) not in ("MIC_ERROR", "ERROR") and json.loads(await ev("JSON.stringify([innerWidth,innerHeight])")) == size_before))
    d = diag()
    results.append(ok("backend knows: microphone_muted", d.get("microphone_muted") is True and d["mic"]["health"] == "MUTED", str(d["mic"]["health"])))
    m0 = d["mic"]["frames"]
    time.sleep(2.0)
    d2 = diag()
    results.append(ok("muted: NO frames reach the voice session", d2["mic"]["frames"] == m0, f"{m0} -> {d2['mic']['frames']}"))
    results.append(ok("muted: nothing sent to Gemini, session not opened by the mic", d2["mic"]["sent_to_gemini"] == d["mic"]["sent_to_gemini"]))

    if "--persist" in sys.argv:
        print("Left MUTED for the restart test. Restart the app, then run: python tools/verify_mute_live.py --after")
        print(f"\n{sum(results)}/{len(results)} checks passed")
        return

    await click_button()
    results.append(ok("second click unmutes", await native() is False and await label() == "Mute microphone"))
    time.sleep(1.5)
    d3 = diag()
    results.append(ok("unmute restores frames without restarting", d3["mic"]["frames"] > d2["mic"]["frames"] + 20, f"{d2['mic']['frames']} -> {d3['mic']['frames']}"))
    results.append(ok("mic health healthy again", d3["mic"]["health"] in ("CONNECTED", "STARTING"), d3["mic"]["health"]))
    print(f"\n{sum(results)}/{len(results)} checks passed")


asyncio.run(main())
