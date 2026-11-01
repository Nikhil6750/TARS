"""Measure Market WebSocket first-frame latency on a real Uvicorn server.

The server uses the integrated backend and real recorded candle store.  The
only instrumentation is a wrapper around ``_build_frame`` which counts builds
and records their durations; it does not change scheduling or results.

Normal connections are measured first.  The stress phase then opens and
abruptly aborts many clients while their first frames are being built, connects
one fresh client immediately, and samples outstanding handler/build work until
the new client receives a frame and the server becomes idle.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import psutil
import uvicorn
import websockets

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "apps" / "backend"
DEFAULT_ARTIFACT_DIR = REPO / "artifacts" / "market-ws-performance"


@dataclass
class BuildMetrics:
    lock: threading.Lock = field(default_factory=threading.Lock)
    started: int = 0
    finished: int = 0
    active: int = 0
    peak_active: int = 0
    durations: list[float] = field(default_factory=list)

    def begin(self) -> float:
        with self.lock:
            self.started += 1
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
        return time.perf_counter()

    def end(self, started_at: float) -> None:
        duration = time.perf_counter() - started_at
        with self.lock:
            self.active -= 1
            self.finished += 1
            self.durations.append(duration)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            durations = list(self.durations)
            return {
                "started": self.started,
                "finished": self.finished,
                "active": self.active,
                "peak_active": self.peak_active,
                "duration_min_seconds": min(durations) if durations else None,
                "duration_mean_seconds": sum(durations) / len(durations) if durations else None,
                "duration_max_seconds": max(durations) if durations else None,
            }


class ErrorCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        rendered = self.format(record)
        if record.exc_info:
            rendered += "\n" + logging.Formatter().formatException(record.exc_info)
        self.records.append(rendered)


def _free_port(port: int) -> None:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise SystemExit(f"port {port} is not free: {exc}") from exc


def _wait_health(url: str, timeout: float = 180.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=3) as response:
                return json.loads(response.read())
        except (OSError, ValueError) as exc:
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError(f"backend did not become healthy: {last_error}")


async def _first_frame(url: str, timeout: float) -> dict[str, Any]:
    total_started = time.perf_counter()
    connection = await asyncio.wait_for(websockets.connect(url, max_size=None), timeout=timeout)
    connected = time.perf_counter()
    try:
        raw = await asyncio.wait_for(connection.recv(), timeout=timeout)
        received = time.perf_counter()
        payload = json.loads(raw)
        return {
            "connect_seconds": connected - total_started,
            "frame_after_connect_seconds": received - connected,
            "total_seconds": received - total_started,
            "frame_type": payload.get("type"),
            "status": (payload.get("status") or {}).get("status"),
            "source": (payload.get("provenance") or {}).get("source"),
            "is_live": (payload.get("provenance") or {}).get("is_live"),
        }
    finally:
        await connection.close()


async def _wait_idle(server: uvicorn.Server, metrics: BuildMetrics, timeout: float) -> float:
    started = time.perf_counter()
    deadline = started + timeout
    while time.perf_counter() < deadline:
        state = metrics.snapshot()
        if (
            not server.server_state.connections
            and not server.server_state.tasks
            and state["active"] == 0
            and state["started"] == state["finished"]
        ):
            return time.perf_counter() - started
        await asyncio.sleep(0.05)
    return time.perf_counter() - started


def _sample(server: uvicorn.Server, metrics: BuildMetrics, process: psutil.Process) -> dict[str, Any]:
    return {
        "elapsed_seconds": 0.0,
        "connections": len(server.server_state.connections),
        "handler_tasks": len(server.server_state.tasks),
        "process_threads": process.num_threads(),
        "process_cpu_seconds": sum(process.cpu_times()[:2]),
        **metrics.snapshot(),
    }


async def _abort_client(url: str, delay: float) -> str | None:
    try:
        connection = await websockets.connect(url, max_size=None, open_timeout=30)
        await asyncio.sleep(delay)
        connection.transport.abort()
        return None
    except Exception as exc:  # noqa: BLE001 - every failed storm connection is evidence
        return f"{type(exc).__name__}: {exc}"


async def _measure(
    *,
    ws_url: str,
    server: uvicorn.Server,
    metrics: BuildMetrics,
    storm_clients: int,
    abort_delay: float,
    timeout: float,
) -> dict[str, Any]:
    process = psutil.Process()
    normal: list[dict[str, Any]] = []
    for _ in range(3):
        normal.append(await _first_frame(ws_url, timeout))
        await _wait_idle(server, metrics, timeout)

    reconnect_first = await _first_frame(ws_url, timeout)
    await _wait_idle(server, metrics, timeout)
    reconnect_second = await _first_frame(ws_url, timeout)
    await _wait_idle(server, metrics, timeout)

    before_storm = metrics.snapshot()
    storm_started = time.perf_counter()
    errors = await asyncio.gather(
        *[_abort_client(ws_url, abort_delay) for _ in range(storm_clients)]
    )
    storm_abort_seconds = time.perf_counter() - storm_started
    abort_errors = [error for error in errors if error]

    probe_started = time.perf_counter()
    probe = asyncio.create_task(_first_frame(ws_url, timeout))
    samples: list[dict[str, Any]] = []
    while not probe.done():
        sample = _sample(server, metrics, process)
        sample["elapsed_seconds"] = time.perf_counter() - probe_started
        samples.append(sample)
        await asyncio.sleep(0.25)
    stressed_reconnect = await probe
    stressed_reconnect["sampled_first_frame_seconds"] = time.perf_counter() - probe_started

    settle_seconds = await _wait_idle(server, metrics, timeout)
    after_settle = _sample(server, metrics, process)
    after_settle["elapsed_seconds"] = settle_seconds
    return {
        "normal_connections": normal,
        "immediate_reconnect_without_storm": {
            "first": reconnect_first,
            "second": reconnect_second,
        },
        "storm": {
            "requested_clients": storm_clients,
            "abort_delay_seconds": abort_delay,
            "abort_phase_seconds": storm_abort_seconds,
            "client_errors": abort_errors,
            "builds_before": before_storm,
            "at_fresh_probe": samples[0] if samples else _sample(server, metrics, process),
            "stressed_reconnect": stressed_reconnect,
            "samples": samples,
            "settle_seconds_after_probe": settle_seconds,
            "after_settle": after_settle,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8781)
    parser.add_argument("--storm-clients", type=int, default=120)
    parser.add_argument("--abort-delay", type=float, default=0.02)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--interactive-target", type=float, default=5.0)
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    args = parser.parse_args()

    _free_port(args.port)
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.artifact_dir / "uvicorn.log"
    report_path = args.artifact_dir / "reconnect-latency.json"

    # Keep the run isolated from user state while retaining the actual recorded
    # store selected by research.config.resolve_data_root().
    os.environ.setdefault("TARS_HOME", str(args.artifact_dir / "home"))
    os.environ.setdefault("USE_MOCK_TRADING_EVENTS", "false")
    os.environ.setdefault("BACKEND_PORT", str(args.port))
    sys.path.insert(0, str(BACKEND))

    from app.main import app
    from app.routers import market_xray

    metrics = BuildMetrics()
    real_build = market_xray._build_frame

    def measured_build(service, timeframe):
        started_at = metrics.begin()
        try:
            return real_build(service, timeframe)
        finally:
            metrics.end(started_at)

    market_xray._build_frame = measured_build

    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.addHandler(file_handler)
    errors = ErrorCapture()
    errors.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root.addHandler(errors)

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=args.port,
        log_config=None,
        ws="websockets",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="market-ws-perf-server", daemon=True)
    thread.start()
    health_url = f"http://127.0.0.1:{args.port}/api/v1/health"
    market_health_url = f"http://127.0.0.1:{args.port}/api/v1/market/health"
    ws_url = f"ws://127.0.0.1:{args.port}/api/v1/market/ws"

    try:
        health = _wait_health(health_url)
        market_health = _wait_health(market_health_url)
        measurements = asyncio.run(
            _measure(
                ws_url=ws_url,
                server=server,
                metrics=metrics,
                storm_clients=args.storm_clients,
                abort_delay=args.abort_delay,
                timeout=args.timeout,
            )
        )
        normal = [item["frame_after_connect_seconds"] for item in measurements["normal_connections"]]
        stressed = measurements["storm"]["stressed_reconnect"]["frame_after_connect_seconds"]
        report = {
            "backend_identity": health.get("identity"),
            "trading_execution": market_health.get("trading_execution"),
            "interactive_target_seconds": args.interactive_target,
            "normal_first_frame_min_seconds": min(normal),
            "normal_first_frame_mean_seconds": sum(normal) / len(normal),
            "normal_first_frame_max_seconds": max(normal),
            "stressed_first_frame_seconds": stressed,
            "meets_interactive_target_normally": max(normal) <= args.interactive_target,
            "meets_interactive_target_after_storm": stressed <= args.interactive_target,
            "builds": metrics.snapshot(),
            "server_errors": errors.records,
            **measurements,
        }
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({
            "normal_first_frame_seconds": normal,
            "stressed_first_frame_seconds": stressed,
            "storm_peak_active_builds": report["builds"]["peak_active"],
            "storm_settle_seconds_after_probe": measurements["storm"]["settle_seconds_after_probe"],
            "server_errors": len(errors.records),
            "report": str(report_path),
        }, indent=2))
        return 0 if not errors.records and stressed <= args.interactive_target else 1
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        root.removeHandler(errors)
        root.removeHandler(file_handler)
        file_handler.close()


if __name__ == "__main__":
    raise SystemExit(main())
