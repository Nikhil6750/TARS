"""Black-box verification of backend ownership, against the real release binary.

Each scenario puts something real on the backend port, starts the desktop shell
and checks what it did, using the process table and the HTTP API rather than
anything the shell says about itself. The rule under test: TARS may use a
compatible backend, may only ever stop one it started, and leaves everything
else alone.

    python tools/verify_backend_ownership.py
    python tools/verify_backend_ownership.py --only foreign_200 orphan_reclaim

A non-default port keeps this away from anything on 8000. Only processes this
script started are ever stopped. It refuses to run while TARS is already up,
because the single-instance plugin would route its launches to that copy.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil

REPO = Path(__file__).resolve().parents[1]
DEFAULT_EXE = REPO / "apps" / "web" / "src-tauri" / "target" / "release" / "tars-companion.exe"
OUT = REPO / "artifacts" / "market-desktop" / "ownership-verification.json"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
BACKEND_START_SECONDS = 150
PROCESS_NAME = "tars-companion.exe"
MARKET_TITLE = "Market Intelligence"

results: list[dict] = []


def record(scenario: str, check: str, passed: bool | None, detail: str = "") -> None:
    verdict = "UNVERIFIED" if passed is None else ("PASS" if passed else "FAIL")
    results.append({"scenario": scenario, "check": check, "result": verdict, "detail": detail})
    print(f"  [{verdict}] {check}" + (f"\n         {detail}" if detail else ""), flush=True)


# ---- what is on the port -------------------------------------------------
def listeners(port: int = PORT) -> list[int]:
    return sorted({
        c.pid for c in psutil.net_connections("inet")
        if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN and c.pid
    })


def health(timeout: float = 3.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"{BASE}/api/v1/health", timeout=timeout) as response:
            return json.loads(response.read())
    except (OSError, ValueError):
        return None


def wait_for(predicate, seconds: float, step: float = 1.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


def shells() -> list[psutil.Process]:
    return [p for p in psutil.process_iter(["name"]) if p.info["name"] == PROCESS_NAME]


# ---- things to put on the port -------------------------------------------
class FakeServer:
    """A real listening HTTP server that is not a compatible TARS backend, and
    remembers everything anyone asked it to do."""

    def __init__(self, body: bytes, status: int = 200):
        self.posts: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                except ConnectionError:
                    pass  # the shell timed out and hung up, which is its business

            def do_POST(self):
                outer.posts.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def alive(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=2):
                return True
        except OSError:
            return False

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class SilentListener:
    """Accepts connections and never says anything."""

    def __init__(self):
        self.socket = socket.socket()
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", PORT))
        self.socket.listen(16)

    def close(self) -> None:
        self.socket.close()


class Harness:
    def __init__(self, exe: Path, backend_dir: Path):
        self.exe = exe
        self.backend_dir = backend_dir
        self.env = {
            **os.environ,
            "TARS_BACKEND_URL": BASE,
            "TARS_BACKEND_DIR": str(backend_dir),
        }
        self.env.pop("TARS_OPEN_MARKET", None)

    def launch(self, *args: str) -> subprocess.Popen:
        return subprocess.Popen([str(self.exe), *args], env=self.env, close_fds=True)

    def quit(self) -> None:
        if shells():
            self.launch("--quit")
            wait_for(lambda: not shells(), 30)

    def manual_backend(self) -> subprocess.Popen:
        env = {**self.env, "BACKEND_PORT": str(PORT)}
        for key in ("TARS_SUPERVISOR_OWNER_ID", "TARS_SUPERVISOR_SECRET"):
            env.pop(key, None)
        return subprocess.Popen(
            [sys.executable, "run.py"], cwd=self.backend_dir, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


def identity() -> dict:
    return (health() or {}).get("identity") or {}


# ---- scenarios -----------------------------------------------------------
def fresh_spawn(h: Harness) -> None:
    h.launch()
    wait_for(lambda: identity().get("owner_id"), BACKEND_START_SECONDS)
    ident = identity()
    record("fresh_spawn", "Spawns a backend when the port is free", bool(ident), str(ident.get("build_id")))
    record("fresh_spawn", "That backend is provably this shell's", bool(ident.get("owner_id")))
    owners = listeners()
    record("fresh_spawn", "Exactly one listener", len(owners) == 1, str(owners))
    if owners and shells():
        parent = psutil.Process(owners[0]).ppid()
        record("fresh_spawn", "Backend is a child of the shell", parent == shells()[0].pid,
               f"parent {parent}, shell {shells()[0].pid}")

    # Anyone can reach loopback; only the owner may stop it.
    request = urllib.request.Request(f"{BASE}/api/v1/runtime/shutdown", method="POST", data=b"{}")
    try:
        urllib.request.urlopen(request, timeout=5)
        code = 200
    except urllib.error.HTTPError as err:
        code = err.code
    except OSError as err:
        code = str(err)
    record("fresh_spawn", "A shutdown without the secret is refused", code == 403, f"HTTP {code}")
    record("fresh_spawn", "It is still running after that attempt", bool(wait_for(health, 3)))

    h.quit()
    gone = wait_for(lambda: not listeners(), 20)
    record("fresh_spawn", "--quit stops the backend it owns", bool(gone), f"listeners {listeners()}")


def attach_compatible(h: Harness) -> None:
    manual = h.manual_backend()
    try:
        wait_for(health, BACKEND_START_SECONDS)
        instance = identity().get("instance_id")
        record("attach_compatible", "A hand-started backend is up", bool(instance))
        h.launch()
        time.sleep(15)
        record("attach_compatible", "No duplicate is spawned", listeners() == [manual.pid],
               f"listeners {listeners()}, manual {manual.pid}")
        record("attach_compatible", "The same process keeps serving",
               identity().get("instance_id") == instance)
        h.quit()
        time.sleep(4)
        record("attach_compatible", "--quit leaves the backend it did not start",
               manual.poll() is None and identity().get("instance_id") == instance,
               f"manual alive={manual.poll() is None}")
    finally:
        manual.terminate()
        wait_for(lambda: not listeners(), 15)


def conflict(h: Harness, name: str, body: bytes, status: int = 200) -> None:
    fake = FakeServer(body, status)
    try:
        h.launch()
        time.sleep(25)
        children = [p.pid for s in shells() for p in s.children(recursive=True)
                    if "python" in p.name().lower()]
        record(name, "No backend is spawned onto an occupied port", not children, f"python children {children}")
        record(name, "The occupant is still running", fake.alive())
        record(name, "The occupant was not told to shut down", not fake.posts, str(fake.posts))
        h.quit()
        time.sleep(3)
        record(name, "--quit leaves it alone too", fake.alive() and not fake.posts, str(fake.posts))
    finally:
        h.quit()
        fake.close()


def foreign_200(h: Harness) -> None:
    conflict(h, "foreign_200", b'{"status":"ok"}')


def foreign_500(h: Harness) -> None:
    conflict(h, "foreign_500", b"nope", status=500)


def incompatible_contract(h: Harness) -> None:
    body = json.dumps({"status": "ok", "identity": {
        "service": "tars-backend", "contract_version": 999, "instance_id": "x", "owner_id": "someone-else",
    }}).encode()
    conflict(h, "incompatible_contract", body)


def silent_listener(h: Harness) -> None:
    listener = SilentListener()
    try:
        h.launch()
        time.sleep(25)
        children = [p.pid for s in shells() for p in s.children(recursive=True)
                    if "python" in p.name().lower()]
        record("silent_listener", "No backend is spawned", not children, str(children))
        h.quit()
        record("silent_listener", "The shell still quits", not shells())
    finally:
        h.quit()
        listener.close()


def orphan_reclaim(h: Harness) -> None:
    shell = h.launch()
    wait_for(lambda: identity().get("owner_id"), BACKEND_START_SECONDS)
    before = identity()
    backend_pid = listeners()[0] if listeners() else None
    shell.kill()
    shell.wait(10)
    survived = wait_for(lambda: backend_pid is not None and psutil.pid_exists(backend_pid) and health(), 20)
    record("orphan_reclaim", "A hard kill leaves the backend running (known limit)", bool(survived),
           f"pid {backend_pid} exists={backend_pid is not None and psutil.pid_exists(backend_pid)}")

    h.launch()
    time.sleep(15)
    after = identity()
    record("orphan_reclaim", "The next launch adopts rather than duplicates",
           after.get("instance_id") == before.get("instance_id") and len(listeners()) == 1,
           f"listeners {listeners()}")
    h.quit()
    gone = wait_for(lambda: not listeners(), 20)
    record("orphan_reclaim", "Having proved ownership, it can stop the orphan", bool(gone), f"listeners {listeners()}")


def cold_quit(h: Harness) -> None:
    process = h.launch("--quit")
    try:
        code = process.wait(20)
    except subprocess.TimeoutExpired:
        code = None
    time.sleep(3)
    record("cold_quit", "--quit with nothing running exits", code is not None, f"exit {code}")
    record("cold_quit", "and starts nothing", not shells() and not listeners())
    h.quit()


def window_titles(pid: int) -> list[tuple[int, str]]:
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    found: list[tuple[int, str]] = []
    proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def visit(hwnd, _):
        owner = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            buffer = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buffer, 256)
            if buffer.value:
                found.append((hwnd, buffer.value))
        return True

    user32.EnumWindows(proc_type(visit), 0)
    return found


def market_windows() -> list[tuple[int, str]]:
    return [w for s in shells() for w in window_titles(s.pid) if MARKET_TITLE in w[1]]


def cold_market(h: Harness) -> None:
    h.launch("--market")
    opened = wait_for(market_windows, 60)
    record("cold_market", "--market from cold opens the Market window", bool(opened), str(opened))
    first = list(shells())
    h.launch("--market")
    # The second process exists briefly while it hands over to the first.
    settled = wait_for(lambda: len(shells()) == 1, 30)
    record("cold_market", "A second --market starts no second process",
           bool(settled) and [p.pid for p in shells()] == [p.pid for p in first],
           str([p.pid for p in shells()]))
    record("cold_market", "and no second window", len(market_windows()) == 1, str(market_windows()))
    wait_for(lambda: identity().get("owner_id"), BACKEND_START_SECONDS)
    connected = wait_for(webview_on_port, 60, 3)
    record("cold_market", "The webview talks to the configured port, not the default",
           bool(connected), f"port {PORT}")
    h.quit()
    wait_for(lambda: not listeners(), 20)


def rapid_double_launch(h: Harness) -> None:
    """Two launches a second apart once deadlocked the whole app: the second
    reached a window still being built, on the main thread."""
    h.launch("--market")
    time.sleep(1)
    h.launch("--market")
    settled = wait_for(lambda: len(shells()) == 1 and len(market_windows()) == 1, 60)
    record("rapid_double_launch", "One process and one Market window", bool(settled),
           f"processes {[p.pid for p in shells()]}, windows {market_windows()}")
    h.launch("--market")
    still_one = wait_for(lambda: len(shells()) == 1, 20)
    record("rapid_double_launch", "The app still responds to a later launch", bool(still_one),
           str([p.pid for p in shells()]))
    h.launch("--quit")
    gone = wait_for(lambda: not shells(), 40)
    record("rapid_double_launch", "and can still be quit", bool(gone), str([p.pid for p in shells()]))


def webview_on_port() -> bool:
    connections = psutil.net_connections("inet")
    for shell in shells():
        pids = {p.pid for p in shell.children(recursive=True)}
        for conn in connections:
            if conn.pid in pids and conn.raddr and conn.raddr.port == PORT:
                return True
    return False


SCENARIOS = {
    "fresh_spawn": fresh_spawn,
    "attach_compatible": attach_compatible,
    "foreign_200": foreign_200,
    "foreign_500": foreign_500,
    "incompatible_contract": incompatible_contract,
    "silent_listener": silent_listener,
    "orphan_reclaim": orphan_reclaim,
    "cold_quit": cold_quit,
    "cold_market": cold_market,
    "rapid_double_launch": rapid_double_launch,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--exe", type=Path, default=DEFAULT_EXE)
    parser.add_argument("--backend-dir", type=Path, default=REPO / "apps" / "backend")
    parser.add_argument("--only", nargs="*", choices=list(SCENARIOS))
    args = parser.parse_args()

    if shells():
        print("TARS is already running; this needs to start its own. Quit it first.")
        return 2
    if listeners():
        print(f"Port {PORT} is in use by {listeners()}; free it first.")
        return 2

    harness = Harness(args.exe, args.backend_dir)
    for name, scenario in SCENARIOS.items():
        if args.only and name not in args.only:
            continue
        print(f"\n== {name} ==", flush=True)
        try:
            scenario(harness)
        except Exception as exc:  # noqa: BLE001 - one scenario must not hide the rest
            record(name, "Scenario completed", None, f"{type(exc).__name__}: {exc}")
        finally:
            harness.quit()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2), encoding="utf-8")
    counts = {v: sum(r["result"] == v for r in results) for v in ("PASS", "FAIL", "UNVERIFIED")}
    print(f"\n==== {counts['PASS']} passed, {counts['FAIL']} failed, {counts['UNVERIFIED']} unverified ====")
    print(f"evidence -> {OUT}")
    return 1 if counts["FAIL"] or counts["UNVERIFIED"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
