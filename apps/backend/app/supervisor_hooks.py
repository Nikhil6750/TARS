"""Tiny bridge between run.py's uvicorn.Server instance and the
`POST /api/v1/runtime/shutdown` route (app/routers/lifecycle.py) -- mission:
FINAL INFRASTRUCTURE MISSION, section 6/8 ("clean intentional shutdown must
NOT trigger auto-restart"). Setting `Server.should_exit = True` triggers
uvicorn's own graceful shutdown path (runs app/main.py's lifespan `finally`
block, then exits cleanly) -- this is cooperative, not a forceful kill, so
the Rust supervisor can tell "I asked it to stop" (this endpoint responded)
apart from "it died on its own" (the HTTP call never got a response/the
process vanished)."""
from __future__ import annotations

_server = None  # uvicorn.Server, set by run.py; None if run under a reload-mode launcher


def set_server(server) -> None:
    global _server
    _server = server


def request_graceful_shutdown() -> bool:
    """Returns True if a shutdown was actually requested (a server
    reference exists), False if there's nothing to signal (e.g. running
    under uvicorn's own --reload multiprocess mode, which this hook does
    not support -- dev-only, never the supervised production path)."""
    if _server is None:
        return False
    _server.should_exit = True
    return True
