"""Entrypoint for running the TARS backend with uvicorn, honoring the
connectivity settings in app.config (see ARCHITECTURE.md § Connectivity):

- Default: binds 127.0.0.1 only — not reachable from any other device.
- BIND_LAN=true: binds 0.0.0.0 — reachable from other devices on the same
  LAN. Still never public — no port-forwarding or Funnel is configured by
  this app.
- Preferred private remote access (e.g. laptop -> iPhone) is Tailscale
  Serve, pointed at this process's localhost address — it does not require
  BIND_LAN, since Tailscale Serve reaches a loopback-bound service over the
  tailnet on its own.

Usage: `python run.py` (equivalent to `uvicorn app.main:app --host ... `
with the host/port read from Settings instead of passed on the command
line, so `.env` stays the single source of truth for how the server binds).
"""
from __future__ import annotations

import uvicorn

from app.config import get_settings

if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.effective_host,
        port=settings.backend_port,
        reload=settings.tars_env == "development",
    )
