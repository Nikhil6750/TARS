"""Run every mandatory TARS V1 certification quality gate."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "apps" / "backend"
WEB = ROOT / "apps" / "web"


@dataclass(frozen=True)
class Gate:
    name: str
    command: tuple[str, ...]
    cwd: Path = ROOT


def default_gates(args: argparse.Namespace) -> list[Gate]:
    python = sys.executable
    gates = [
        Gate(
            "contracts and certification tests",
            (
                python,
                "-m",
                "pytest",
                "tests/contracts",
                "tests/unit",
                "tests/integration",
                "tests/certification",
                "-q",
            ),
        ),
        Gate("contract codegen drift", (python, "tools/generate_contracts.py", "--check")),
        Gate("backend pytest", (python, "-m", "pytest", "tests", "-q"), BACKEND),
        Gate(
            "Ruff",
            (python, "-m", "ruff", "check", "apps/backend", "tests", "tools"),
        ),
        Gate(
            "MyPy",
            (
                python,
                "-m",
                "mypy",
                "app",
                "assistant",
                "events",
                "memory",
                "storage",
                "voice",
            ),
            BACKEND,
        ),
        Gate("frontend install", ("npm", "ci"), WEB),
        Gate("frontend tests", ("npm", "test"), WEB),
        Gate("frontend TypeScript", ("npm", "run", "typecheck"), WEB),
        Gate("frontend lint", ("npm", "run", "lint"), WEB),
        Gate("frontend production build", ("npm", "run", "build"), WEB),
        Gate(
            "Tauri compatibility",
            (python, "tools/tauri_checks.py", "--cargo-check"),
        ),
    ]
    if shutil.which("cargo") and shutil.which("rustc"):
        gates.append(
            Gate(
                "Tauri native build",
                ("npm", "run", "tauri", "--", "build", "--debug", "--no-bundle"),
                WEB,
            )
        )

    acceptance = [
        python,
        "tools/run_acceptance.py",
        "--backend-command",
        args.backend_command,
        "--frontend-command",
        args.frontend_command,
        "--backend-cwd",
        str(args.backend_cwd),
        "--frontend-cwd",
        str(args.frontend_cwd),
        "--base-url",
        args.base_url,
        "--frontend-url",
        args.frontend_url,
        "--startup-timeout",
        str(args.startup_timeout),
    ]
    gates.append(Gate("external acceptance", tuple(acceptance)))
    return gates


def run_gate(gate: Gate) -> int:
    display = " ".join(shlex.quote(part) for part in gate.command)
    print(f"\n=== {gate.name} ===\n{display}", flush=True)
    command = list(gate.command)
    executable = shutil.which(command[0])
    if executable is not None:
        command[0] = executable
    if os.name == "nt" and command[0].casefold().endswith((".cmd", ".bat")):
        command = [os.environ.get("COMSPEC", "cmd.exe"), "/c", *command]
    try:
        return subprocess.run(command, cwd=gate.cwd, check=False).returncode
    except FileNotFoundError as exc:
        print(f"gate executable missing: {exc}", file=sys.stderr)
        return 127


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend-command",
        default=os.getenv(
            "TARS_BACKEND_COMMAND",
            "python -m uvicorn app.main:app --host 127.0.0.1 --port 8000",
        ),
    )
    parser.add_argument(
        "--frontend-command",
        default=os.getenv(
            "TARS_FRONTEND_COMMAND", "npm run dev -- --host 127.0.0.1 --port 5173"
        ),
    )
    parser.add_argument("--backend-cwd", type=Path, default=BACKEND)
    parser.add_argument("--frontend-cwd", type=Path, default=WEB)
    parser.add_argument(
        "--base-url", default=os.getenv("TARS_BASE_URL", "http://127.0.0.1:8000")
    )
    parser.add_argument(
        "--frontend-url",
        default=os.getenv("TARS_FRONTEND_URL", "http://127.0.0.1:5173"),
    )
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    results = [(gate.name, run_gate(gate)) for gate in default_gates(args)]
    print("\n=== certification summary ===")
    for name, returncode in results:
        print(f"{'PASS' if returncode == 0 else 'FAIL'} {name} (exit {returncode})")
    raise SystemExit(1 if any(returncode for _, returncode in results) else 0)


if __name__ == "__main__":
    main()
