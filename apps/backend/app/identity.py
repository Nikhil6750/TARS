"""Backend build and process identity.

The desktop shell supervises this backend by polling ``/api/v1/health`` and
adopting whatever answers there (``apps/web/src-tauri/src/supervisor.rs``,
adopt-or-spawn). A 200 alone does not say *what* answered: an orphan backend
left behind by a previous, older build serves that endpoint just as happily
as the one the shell expects, and adopting it silently runs a new frontend
against old backend code.

This module supplies the two values that make the difference checkable:

``instance_id``
    Identifies this *process*. Generated once at import, so it is stable for
    the life of the process and different in every new one. It lets a
    supervisor notice that the backend it is talking to is not the backend it
    started, even when both report the same build.

``build_id``
    Identifies this *code*. The commit SHA when the repository is readable,
    which is the only value that genuinely changes whenever the code does.
    Where there is no repository -- an installed build -- it falls back to
    the release version, and ``build_source`` says which of the two you are
    looking at, so a fallback is never mistaken for a real SHA.

Nothing here raises. Identity is diagnostic: a backend that cannot work out
its own build must still serve health, or the supervisor's health poll would
fail over a missing `git`, which would be a worse outcome than an unknown
build id.
"""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

# The release version of the whole application. The desktop shell is versioned
# in `apps/web/src-tauri/tauri.conf.json` and `apps/web/package.json`; this
# must match, because a supervisor comparing its own version against the
# backend's is comparing these two numbers. `test_identity.py` asserts they
# have not drifted apart.
APP_VERSION = "1.0.0"

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Generated at import, i.e. once per process start.
_INSTANCE_ID = uuid.uuid4().hex[:16]
_STARTED_AT = datetime.now(UTC)


def _git_commit() -> str | None:
    """The current commit, or None when there is no readable repository.

    Deliberately cheap and silent: a 2s cap and a swallowed failure, because
    this runs behind the endpoint the supervisor polls every 5 seconds.
    """
    if not (_REPO_ROOT / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and len(sha) == 40 else None


@lru_cache(maxsize=1)
def build_identity() -> tuple[str, str]:
    """``(build_id, build_source)``.

    Cached: the commit cannot change under a running process in any way that
    matters here -- the loaded modules are the ones imported at startup, so
    re-reading HEAD after a checkout would report code this process is not
    actually running.
    """
    env = os.environ.get("TARS_BUILD_ID", "").strip()
    if env:
        return env, "env"
    sha = _git_commit()
    if sha:
        return sha[:12], "git"
    return APP_VERSION, "version"


def instance_id() -> str:
    """Identifies this process. Stable within it, unique across processes."""
    return _INSTANCE_ID


def started_at() -> datetime:
    return _STARTED_AT


def identity() -> dict[str, str]:
    """The identity block served inside the health payload."""
    build_id, build_source = build_identity()
    return {
        "instance_id": _INSTANCE_ID,
        "build_id": build_id,
        "build_source": build_source,
        "app_version": APP_VERSION,
        "started_at": _STARTED_AT.isoformat(),
    }


def shell_version() -> str | None:
    """The desktop shell's declared version, read from its Tauri config.

    Only for the drift test below and for diagnostics -- the running backend
    never depends on `apps/web/` being present.
    """
    conf = _REPO_ROOT / "apps" / "web" / "src-tauri" / "tauri.conf.json"
    if not conf.exists():
        return None
    try:
        version = json.loads(conf.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        return None
    return str(version) if version else None
