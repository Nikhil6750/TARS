"""Prove a local CDP listener belongs to the explicitly selected Tauri package."""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlparse

import psutil


def require_packaged_webview(cdp: str, app: Path) -> dict:
    address = urlparse(cdp)
    assert address.hostname in ("127.0.0.1", "localhost"), "CDP must be local"
    listeners = [c for c in psutil.net_connections("tcp")
                 if c.status == psutil.CONN_LISTEN and c.laddr.port == address.port]
    assert len(listeners) == 1, "Expected one local WebView2 listener"
    process = psutil.Process(listeners[0].pid)
    assert Path(process.exe()).name.lower() == "msedgewebview2.exe", process.exe()
    parents = process.parents()
    owner = next((p for p in parents if Path(p.exe()) == app.resolve()), None)
    assert owner is not None, "WebView2 is not owned by the supplied package"
    return {"app": str(app.resolve()), "sha256": hashlib.sha256(app.read_bytes()).hexdigest(),
            "shell_pid": owner.pid, "webview_pid": process.pid, "webview_exe": process.exe()}
