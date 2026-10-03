from __future__ import annotations

import sys
from pathlib import Path

backend_dir = Path(__file__).resolve().parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

import uvicorn  # noqa: E402 — must follow the sys.path insert above

from app.config import get_settings  # noqa: E402 — must follow the sys.path insert above

if __name__ == "__main__":
    settings = get_settings()
    if settings.tars_backend_reload:
        # Reload mode dispatches through uvicorn's own multiprocess
        # reloader, not a plain Server.run() -- the graceful-shutdown hook
        # below needs a real Server instance, so it's skipped here. This
        # is fine: reload is opt-in, dev-only (see its own docstring in
        # app/config.py), never the Rust-supervised production path.
        uvicorn.run(
            "app.main:app", host=settings.effective_host, port=settings.backend_port, reload=True,
        )
    else:
        from app.supervisor_hooks import set_server  # noqa: E402

        config = uvicorn.Config("app.main:app", host=settings.effective_host, port=settings.backend_port)
        server = uvicorn.Server(config)
        set_server(server)
        server.run()
