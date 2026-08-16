"""Launch TARS applications and run black-box acceptance verification."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import psutil

ROOT = Path(__file__).resolve().parents[1]
PAID_KEY_NAMES = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "FISH_AUDIO_API_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "QUANT_BRAIN_API_KEY",
)


@dataclass
class ManagedProcess:
    name: str
    command: str
    cwd: Path
    environment: dict[str, str]
    log_path: Path
    process: subprocess.Popen[bytes] | None = None
    _log_handle: object | None = None

    def start(self) -> None:
        arguments = shlex.split(self.command, posix=os.name != "nt")
        if not arguments:
            raise ValueError(f"{self.name} command is empty")
        cmd_path = shutil.which(arguments[0])
        if cmd_path is None and os.name == "nt":
            for ext in (".cmd", ".bat", ".exe", ".ps1"):
                found = shutil.which(arguments[0] + ext)
                if found:
                    cmd_path = found
                    break
        if cmd_path:
            arguments[0] = cmd_path
        if os.name == "nt" and str(arguments[0]).lower().endswith((".cmd", ".bat")):
            arguments = [os.environ.get("COMSPEC", "cmd.exe"), "/c", *arguments]
        self._log_handle = self.log_path.open("wb")
        self.process = subprocess.Popen(
            arguments,
            cwd=self.cwd,
            env=self.environment,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )

    def assert_running(self) -> None:
        assert self.process is not None
        return_code = self.process.poll()
        if return_code is not None:
            raise RuntimeError(
                f"{self.name} exited with code {return_code}; see {self.log_path}"
            )

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            try:
                root = psutil.Process(self.process.pid)
                descendants = root.children(recursive=True)
                for process in reversed(descendants):
                    process.terminate()
                root.terminate()
                _, alive = psutil.wait_procs(
                    [*descendants, root], timeout=5.0
                )
                for process in alive:
                    process.kill()
                psutil.wait_procs(alive, timeout=2.0)
            except psutil.Error:
                self.process.kill()
            self.process.wait(timeout=3.0)
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None


def wait_for_http(
    url: str, process: ManagedProcess | None, deadline_seconds: float
) -> None:
    deadline = time.monotonic() + deadline_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process is not None:
            process.assert_running()
        try:
            response = httpx.get(url, timeout=1.0)
            if response.status_code < 500:
                return
            last_error = RuntimeError(f"HTTP {response.status_code}")
        except httpx.HTTPError as exc:
            last_error = exc
        threading.Event().wait(min(0.1, max(0.0, deadline - time.monotonic())))
    raise TimeoutError(f"Timed out waiting for {url}: {last_error}")


def assert_endpoint_unused(url: str) -> None:
    """Refuse a process-owning run when a stale service already owns the URL."""

    try:
        response = httpx.get(url, timeout=0.5)
    except httpx.HTTPError:
        return
    raise RuntimeError(
        f"Refusing to launch over an existing service at {url} "
        f"(HTTP {response.status_code})"
    )


def scrubbed_environment(temp_dir: Path, sentinel: str) -> dict[str, str]:
    environment = os.environ.copy()
    for name in PAID_KEY_NAMES:
        environment[name] = ""
    vault = temp_dir / "vault"
    note = vault / "Certification" / "Provenance.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "# Certification provenance anchor\n\n"
        "Project AURORA carries marker TARS_PROVENANCE_ANCHOR.\n"
        "No verified statistical performance facts are recorded here.\n",
        encoding="utf-8",
    )
    environment.update(
        {
            "TARS_ENV": "test",
            "DATABASE_URL": f"sqlite:///{(temp_dir / 'tars.db').as_posix()}",
            "USE_MOCK_TRADING_EVENTS": "false",
            "ASSISTANT_PROVIDER": "mock",
            "WAKE_WORD_PROVIDER": "mock",
            "STT_PROVIDER": "mock",
            "TTS_PROVIDER": "mock",
            "OBSIDIAN_VAULT_PATH": str(vault),
            "TARS_SECRET_SENTINEL": sentinel,
            "TARS_ACCEPTANCE_ZERO_PAID_KEYS": "1",
            "TARS_ACCEPTANCE_VAULT_SOURCE_ID": "Certification/Provenance.md",
        }
    )
    return environment


def scan_logs(log_paths: list[Path], sentinel: str) -> None:
    leaked = [
        path
        for path in log_paths
        if path.exists() and sentinel.encode("utf-8") in path.read_bytes()
    ]
    if leaked:
        locations = ", ".join(str(path) for path in leaked)
        raise RuntimeError(f"Secret sentinel leaked to process logs: {locations}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-command", default=os.getenv("TARS_BACKEND_COMMAND"))
    parser.add_argument("--frontend-command", default=os.getenv("TARS_FRONTEND_COMMAND"))
    parser.add_argument(
        "--backend-cwd", type=Path, default=Path(os.getenv("TARS_BACKEND_CWD", ROOT))
    )
    parser.add_argument(
        "--frontend-cwd", type=Path, default=Path(os.getenv("TARS_FRONTEND_CWD", ROOT))
    )
    parser.add_argument(
        "--base-url", default=os.getenv("TARS_BASE_URL", "http://127.0.0.1:8000")
    )
    parser.add_argument(
        "--frontend-url", default=os.getenv("TARS_FRONTEND_URL", "http://127.0.0.1:5173")
    )
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    parser.add_argument(
        "--use-running-services",
        action="store_true",
        help="run against already-started services (does not prove process startup)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.use_running_services and (
        not args.backend_command or not args.frontend_command
    ):
        raise SystemExit(
            "Full acceptance requires --backend-command and --frontend-command. "
            "Use --use-running-services only for an explicitly partial run."
        )

    sentinel = "TARS-SECRET-MUST-NOT-LEAK-7c0c7f23"
    processes: list[ManagedProcess] = []
    result = 1
    with tempfile.TemporaryDirectory(prefix="tars-acceptance-") as temp_name:
        temp_dir = Path(temp_name)
        environment = scrubbed_environment(temp_dir, sentinel)
        environment.update(
            {
                "TARS_ACCEPTANCE": "1",
                "TARS_BASE_URL": args.base_url,
                "TARS_FRONTEND_URL": args.frontend_url,
            }
        )
        log_paths = [temp_dir / "backend.log", temp_dir / "frontend.log"]
        try:
            if not args.use_running_services:
                health_path = os.getenv("TARS_HEALTH_PATH", "/health")
                assert_endpoint_unused(
                    args.base_url.rstrip("/") + "/" + health_path.lstrip("/")
                )
                assert_endpoint_unused(args.frontend_url)
                backend = ManagedProcess(
                    "backend",
                    args.backend_command,
                    args.backend_cwd.resolve(),
                    environment,
                    log_paths[0],
                )
                backend.start()
                processes.append(backend)
                wait_for_http(
                    args.base_url.rstrip("/") + "/" + health_path.lstrip("/"),
                    backend,
                    args.startup_timeout,
                )

                frontend_environment = environment | {
                    "VITE_TARS_API_URL": args.base_url,
                    "VITE_TARS_WS_URL": args.base_url.replace("http", "ws", 1),
                }
                frontend = ManagedProcess(
                    "frontend",
                    args.frontend_command,
                    args.frontend_cwd.resolve(),
                    frontend_environment,
                    log_paths[1],
                )
                frontend.start()
                processes.append(frontend)
                wait_for_http(args.frontend_url, frontend, args.startup_timeout)
                environment["TARS_ACCEPTANCE_PROCESSES_STARTED"] = "1"
            else:
                wait_for_http(args.base_url.rstrip("/") + "/health", None, 3.0)
                wait_for_http(args.frontend_url, None, 3.0)
                environment["TARS_ACCEPTANCE_PROCESSES_STARTED"] = "0"

            environment["TARS_ACCEPTANCE_LOG_FILES"] = os.pathsep.join(
                str(path) for path in log_paths
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests/acceptance",
                    "-q",
                    "-rA",
                ],
                cwd=ROOT,
                env=environment,
                check=False,
            )
            result = completed.returncode
        finally:
            for process in reversed(processes):
                process.stop()
            try:
                scan_logs(log_paths, sentinel)
            except RuntimeError as exc:
                print(exc, file=sys.stderr)
                result = 1
    raise SystemExit(result)


if __name__ == "__main__":
    main()
