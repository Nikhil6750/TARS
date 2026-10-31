"""Exercise the real NSIS installer, end to end, without a development checkout.

This is NOT a clean-machine test. This machine has Visual Studio, Python and the
rest, and Windows 11 Home has neither Windows Sandbox nor Hyper-V. What it does
establish: the installed copy runs from its own files, with PATH cut down to
Windows, no development variables, and an empty data root. System-level gaps are
covered separately by verify_bundle_dependencies.py and only a real clean install
can close them, so this script says so in its own report.

    python tools/verify_installer.py
    python tools/verify_installer.py --keep     # leave the install for inspection

It installs for the current user into a temporary folder and uninstalls at the end.
Tauri resolves the app data directory through Windows, so TARS_HOME here is the
real %LOCALAPPDATA%\\com.tars.companion, and the installer's Start Menu entry
exists briefly. It refuses to run while TARS is running.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "apps/web/src-tauri/target/release/bundle/nsis/TARS_1.0.0_x64-setup.exe"
OUT = REPO / "artifacts" / "market-desktop"
PORT = 8770
BASE = f"http://127.0.0.1:{PORT}"
STARTUP_SECONDS = 240

results: list[dict] = []


def record(check: str, passed: bool | None, detail: str = "") -> bool:
    verdict = "UNVERIFIED" if passed is None else ("PASS" if passed else "FAIL")
    results.append({"check": check, "result": verdict, "detail": detail})
    print(f"  [{verdict}] {check}" + (f"\n         {detail}" if detail else ""), flush=True)
    return bool(passed)


def get(path: str, timeout: float = 30):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as response:
            return json.loads(response.read())
    except (OSError, ValueError):
        return None


def post(path: str):
    request = urllib.request.Request(BASE + path, method="POST", data=b"{}")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def wait_for(predicate, seconds: float, step: float = 2.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


def shells() -> list[psutil.Process]:
    return [p for p in psutil.process_iter(["name"]) if p.info["name"] == "tars-companion.exe"]


def listeners() -> list[int]:
    return sorted({
        c.pid for c in psutil.net_connections("inet")
        if c.laddr and c.laddr.port == PORT and c.status == psutil.CONN_LISTEN and c.pid
    })


def snapshot(root: Path) -> set[str]:
    return {
        str(p.relative_to(root)) for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }


def isolated_env(data_root: Path, csv_root: Path) -> dict:
    """Windows and nothing else: no Python, Node, Git, Rust or TARS variables."""
    system = os.environ["SYSTEMROOT"]
    # Upper case: os.environ upper-cases its keys on Windows, and losing
    # SYSTEMROOT stops Winsock loading in every child process.
    keep = {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS",
            "PROCESSOR_ARCHITECTURE", "OS", "USERNAME", "USERDOMAIN", "USERPROFILE", "HOMEDRIVE",
            "HOMEPATH", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "PROGRAMFILES",
            "PROGRAMFILES(X86)", "TEMP", "TMP"}
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env["PATH"] = f"{system}\\System32;{system};{system}\\System32\\Wbem"
    env["TARS_BACKEND_URL"] = BASE
    env["TARS_DATA_ROOT"] = str(data_root)
    env["TARS_MARKET_CSV_ROOT"] = str(csv_root)
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--csvs", type=Path, default=Path("C:/Trade Data"),
                        help="real CSV folder for the import step")
    args = parser.parse_args()

    if shells():
        print("TARS is already running; quit it first.")
        return 2
    if not INSTALLER.exists():
        print(f"no installer at {INSTALLER}")
        return 2

    root = Path(tempfile.mkdtemp(prefix="tars-install-"))
    install_dir, data_root, empty_csvs = root / "app", root / "data", root / "no-csvs"
    data_root.mkdir()
    empty_csvs.mkdir()
    env = isolated_env(data_root, empty_csvs)
    size = INSTALLER.stat().st_size / 1e6
    print(f"installer {size:.0f} MB -> {install_dir}\n", flush=True)

    # ---- install -----------------------------------------------------------
    started = time.time()
    code = subprocess.run([str(INSTALLER), "/S", f"/D={install_dir}"], check=False).returncode
    exe = install_dir / "tars-companion.exe"
    record("The installer runs silently and exits 0", code == 0,
           f"exit {code} in {time.time() - started:.0f}s")
    wait_for(exe.exists, 60)
    if not record("The application is installed", exe.exists(), str(exe)):
        return finish(root, args.keep)
    bundled_python = install_dir / "tars" / "python" / "python.exe"
    record("It carries its own Python", bundled_python.exists())
    # Library assets are fine; anything from the application's own tree is not.
    leaked = [n for n in snapshot(install_dir)
              if "site-packages" not in n
              and (n.endswith((".env", ".db", ".parquet", ".wav")) or "storage\tmp" in n)]
    record("No user data or secrets were packaged", not leaked, str(leaked[:5]))
    before = snapshot(install_dir)

    # ---- first launch ------------------------------------------------------
    started = time.time()
    subprocess.Popen([str(exe), "--market"], env=env, close_fds=True, cwd=str(root))
    healthy = wait_for(lambda: (get("/api/v1/health", 5) or {}).get("identity", {}).get("owner_id"),
                       STARTUP_SECONDS)
    record("The installed backend starts by itself", bool(healthy), f"{time.time() - started:.0f}s")
    if not healthy:
        log = Path(os.environ["LOCALAPPDATA"]) / "com.tars.companion" / "logs" / "backend.log"
        tail = log.read_text(errors="replace")[-600:] if log.exists() else "no backend log"
        record("Backend log explains the failure", None, tail)
        return finish(root, args.keep, install_dir)

    identity = get("/api/v1/health")["identity"]
    record("It reports an installed build", identity["build_source"] == "env", identity["build_id"])
    server = psutil.Process(listeners()[0]) if listeners() else None
    running_from = server.exe() if server else ""
    record("It runs on the bundled Python, not the system one",
           Path(running_from).is_relative_to(install_dir), running_from)
    record("It is a child of the installed shell",
           bool(server) and bool(shells()) and server.ppid() == shells()[0].pid)
    record("Trading execution is disabled",
           get("/api/v1/health").get("live_trading_enabled") is False)

    # ---- first run ---------------------------------------------------------
    setup = get("/api/v1/market/setup")
    record("An empty store is reported, not an error",
           bool(setup) and setup["ready"] is False, str(setup and setup["store_root"]))
    record("An empty store does not read as live",
           bool(setup) and setup["status"]["status"] == "NO_DATA", str(setup and setup["status"]["status"]))
    record("The store is on the configured data root",
           bool(setup) and Path(setup["store_root"]).is_relative_to(data_root))

    from verify_backend_ownership import market_windows
    shot_note = "window present" if market_windows() else "no Market window found"
    record("The Market window opened from the installed app", bool(market_windows()), shot_note)
    # The window opens before the backend is up, so give its retry time to land.
    time.sleep(12)
    capture_screenshot("05-installed-first-run.png")

    # ---- import, then offline replay ---------------------------------------
    if args.csvs.is_dir():
        started = time.time()
        post(f"/api/v1/market/import?csv_root={urllib.request.quote(str(args.csvs))}")
        done = wait_for(lambda: (get("/api/v1/market/setup", 10) or {}).get("import", {}).get("state")
                        in ("DONE", "FAILED"), 400)
        state = (get("/api/v1/market/setup") or {}).get("import", {})
        record("The in-app import completes", bool(done) and state.get("state") == "DONE",
               f"{state.get('result') or state.get('error')} in {time.time() - started:.0f}s")
        ready = get("/api/v1/market/setup") or {}
        record("The store is ready afterwards", ready.get("ready") is True,
               f"{ready.get('symbols_in_store')} instruments")
        dashboard = get("/api/v1/market/dashboard?timeframe=H1", 120) or {}
        record("The dashboard serves real panels offline",
               len((dashboard.get("xray") or {}).get("series", [])) > 0,
               f"{len((dashboard.get('xray') or {}).get('series', []))} series")
        record("Imported history is REPLAY, not LIVE",
               (dashboard.get("status") or {}).get("status") == "REPLAY"
               and (dashboard.get("provenance") or {}).get("is_live") is False)
        step = post("/api/v1/market/replay/step?timeframe=H1&bars=-24")
        record("Replay controls work", bool(step.get("cursor")), str(step.get("cursor")))
        time.sleep(6)
        capture_screenshot("06-installed-dashboard.png")
    else:
        record("The in-app import completes", None, f"no CSV folder at {args.csvs}")

    # ---- coexistence and shutdown ------------------------------------------
    from verify_backend_ownership import window_titles
    titles = [t for s in shells() for _, t in window_titles(s.pid)]
    record("The companion runs alongside Market Intelligence",
           any(t == "TARS" or t.startswith("TARS ") for t in titles), str(titles))

    subprocess.Popen([str(exe), "--quit"], env=env, close_fds=True)
    gone = wait_for(lambda: not shells() and not listeners(), 60)
    record("--quit stops the app and its backend", bool(gone),
           f"shells {[p.pid for p in shells()]}, listeners {listeners()}")

    log = Path(os.environ["LOCALAPPDATA"]) / "com.tars.companion" / "logs" / "backend.log"
    record("The backend wrote a log outside the install directory", log.exists(), str(log))

    written = sorted(snapshot(install_dir) - before)
    record("Nothing was written into the install directory", not written, str(written[:6]))
    return finish(root, args.keep, install_dir)


def capture_screenshot(name: str) -> None:
    from capture_native_tars import capture_window, enumerate_windows

    for shell in shells():
        for window in enumerate_windows(shell.pid):
            if "market intelligence" in window.title.lower():
                OUT.mkdir(parents=True, exist_ok=True)
                capture_window(window.hwnd, OUT / name)
                return


def finish(root: Path, keep: bool, install_dir: Path | None = None) -> int:
    if install_dir is not None and not keep:
        for process in shells():
            subprocess.Popen([process.exe(), "--quit"], close_fds=True)
        wait_for(lambda: not shells(), 40)
        uninstaller = install_dir / "uninstall.exe"
        if uninstaller.exists():
            code = subprocess.run([str(uninstaller), "/S"], check=False).returncode
            removed = wait_for(lambda: not (install_dir / "tars-companion.exe").exists(), 120)
            record("The uninstaller removes the application", bool(removed), f"exit {code}")
        home = Path(os.environ["LOCALAPPDATA"]) / "com.tars.companion"
        record("User data survives uninstalling", home.exists(), str(home))
    if not keep:
        shutil.rmtree(root, ignore_errors=True)

    counts = {v: sum(r["result"] == v for r in results) for v in ("PASS", "FAIL", "UNVERIFIED")}
    print(f"\n==== {counts['PASS']} passed, {counts['FAIL']} failed, {counts['UNVERIFIED']} unverified ====")
    print("Not a clean-machine test: this ran on a development machine with an isolated")
    print("environment. A clean Windows install is still required before release.")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "installer-verification.json").write_text(
        json.dumps({"scope": "isolated environment on a development machine, NOT a clean install",
                    "checks": results}, indent=2),
        encoding="utf-8",
    )
    return 1 if counts["FAIL"] or counts["UNVERIFIED"] else 0


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
