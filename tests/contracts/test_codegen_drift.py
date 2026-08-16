"""Generation must be a pure, reproducible function of canonical schemas."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_generated_contracts_have_no_drift() -> None:
    subprocess.run(
        [sys.executable, str(ROOT / "tools" / "generate_contracts.py"), "--check"],
        cwd=ROOT,
        check=True,
    )
