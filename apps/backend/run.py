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
    uvicorn.run(
        "app.main:app",
        host=settings.effective_host,
        port=settings.backend_port,
        reload=settings.tars_backend_reload,
    )
