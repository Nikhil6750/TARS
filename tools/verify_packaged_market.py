"""Non-interactive acceptance of an already running, provably owned package.

No mouse/keyboard automation. The opt-in recovery check kills only the backend
whose executable and parent both belong to the explicitly supplied package.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import time
from pathlib import Path
from urllib.request import Request, urlopen

import psutil
import websockets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--quit-after", action="store_true")
    args = parser.parse_args()
    app = args.app.resolve()
    checks = []

    def check(name, ok, detail):
        checks.append({"name": name, "pass": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
        assert ok, name

    def get(path):
        with urlopen(args.api + path, timeout=10) as response:
            return json.load(response)

    async def frame():
        async with websockets.connect(args.api.replace("http", "ws", 1) + "/api/v1/market/ws",
                                      max_size=None, open_timeout=20) as ws:
            await ws.send(json.dumps({"type": "subscribe", "timeframe": "H4"}))
            value = json.loads(await asyncio.wait_for(ws.recv(), 60))
            return {"type": value.get("type"), "timeframe": value.get("timeframe"),
                    "status": value.get("status", {}).get("status"),
                    "live": value.get("provenance", {}).get("is_live"),
                    "series": len(value.get("xray", {}).get("series", []))}

    try:
        before = get("/api/v1/health")
        identity = before["identity"]
        backend = psutil.Process(identity["pid"])
        shell = backend.parent()
        owned = (Path(backend.exe()).is_relative_to(app.parent)
                 and shell is not None and Path(shell.exe()) == app and identity.get("owner_id"))
        check("Package owns its bundled backend", owned,
              {"identity": identity, "python": backend.exe(), "parent": shell.pid if shell else None})
        check("Trading execution disabled", before.get("live_trading_enabled") is False,
              before.get("live_trading_enabled"))
        result = asyncio.run(frame())
        check("Packaged first H4 frame is populated replay", result["timeframe"] == "H4"
              and result["status"] == "REPLAY" and result["live"] is False and result["series"] > 0,
              result)
        if args.recover:
            began = time.monotonic()
            backend.kill()
            deadline = began + 150
            after = None
            while time.monotonic() < deadline:
                try:
                    candidate = get("/api/v1/health")["identity"]
                    if candidate["instance_id"] != identity["instance_id"]:
                        after = candidate
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(1)
            check("Owned backend recovers", after is not None,
                  {"seconds": round(time.monotonic() - began, 2), "new": after})
            recovered = psutil.Process(after["pid"])
            check("Recovered backend remains owned by original shell",
                  recovered.ppid() == shell.pid and Path(recovered.exe()).is_relative_to(app.parent),
                  {"pid": recovered.pid, "shell": shell.pid})
            result = asyncio.run(frame())
            check("WebSocket frames resume after recovery", result["series"] > 0
                  and result["status"] == "REPLAY" and result["live"] is False, result)
        request = Request(args.api + "/api/v1/market/replay/step?timeframe=H1&bars=-12", method="POST")
        with urlopen(request, timeout=30) as response:
            replay = json.load(response)
        check("Packaged replay steps without LIVE", bool(replay.get("cursor"))
              and replay["status"]["status"] == "REPLAY", replay.get("cursor"))
        if args.quit_after:
            subprocess.run([str(app), "--quit"], check=True, timeout=20)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and shell.is_running():
                time.sleep(1)
            check("Cooperative quit stops the shell", not shell.is_running(), shell.pid)
            try:
                get("/api/v1/health")
                still_serving = True
            except OSError:
                still_serving = False
            check("No orphan backend listener", not still_serving, args.api)
        return 0
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(checks, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
