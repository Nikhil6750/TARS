from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from tools.run_certification import Gate, default_gates, run_gate


def args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        backend_command="backend-command",
        frontend_command="frontend-command",
        backend_cwd=tmp_path,
        frontend_cwd=tmp_path,
        base_url="http://127.0.0.1:8000",
        frontend_url="http://127.0.0.1:5173",
        startup_timeout=5.0,
    )


def test_certification_runner_contains_every_mandatory_gate(tmp_path: Path) -> None:
    gates = default_gates(args(tmp_path))
    names = {gate.name for gate in gates}
    assert {
        "backend pytest",
        "Ruff",
        "MyPy",
        "frontend tests",
        "frontend TypeScript",
        "frontend lint",
        "frontend production build",
        "contract codegen drift",
        "Tauri compatibility",
        "external acceptance",
    } <= names


def test_ruff_gate_is_a_failing_subprocess_not_advisory(tmp_path: Path) -> None:
    ruff = next(gate for gate in default_gates(args(tmp_path)) if gate.name == "Ruff")
    assert ruff.command[:4] == (sys.executable, "-m", "ruff", "check")


def test_gate_propagates_nonzero_result(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=23),
    )
    gate = Gate("failure", (sys.executable, "--version"), tmp_path)
    assert run_gate(gate) == 23
