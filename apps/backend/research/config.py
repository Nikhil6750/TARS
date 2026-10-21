"""Research data root. Historical data must never fill C:."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

SUBDIRS = ("market", "events", "news", "derived", "experiments", "reports", "cache")


def _drive_ok(drive: str, min_free_gb: float = 5.0) -> bool:
    try:
        root = drive + "\\"
        return Path(root).exists() and shutil.disk_usage(root).free / 1e9 >= min_free_gb
    except OSError:
        return False


def resolve_data_root() -> Path:
    """TARS_DATA_ROOT if set; else <non-system drive>:\\TARS_DATA (D: preferred) with >=5GB free;
    else a repo-local artifacts fallback."""
    env = os.environ.get("TARS_DATA_ROOT", "").strip()
    if env:
        return Path(env)
    sysdrive = os.environ.get("SystemDrive", "C:").upper()
    for letter in "DEFGH":
        drive = f"{letter}:"
        if drive != sysdrive and _drive_ok(drive):
            return Path(f"{drive}\\TARS_DATA")
    return Path(__file__).resolve().parents[3] / "artifacts" / "research_data"


def ensure_layout(root: Path | None = None) -> Path:
    root = Path(root) if root else resolve_data_root()
    for sub in SUBDIRS:
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root
