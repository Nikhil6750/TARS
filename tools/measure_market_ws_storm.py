"""The reconnect storms `measure_market_ws_reconnect.py` does not reach.

That tool's probe arrives after the storm's only frame is finished, so with
frames shared it measures a kept frame. These scenarios measure the harder
cases on the same real Uvicorn server and recorded store, and also run against
a build with no frame sharing, which is how the before figures are taken:

cold        The cursor moves, then a storm, then a probe. Nothing is kept.
timeframes  As `cold`, with the storm spread across every timeframe and the
            probe on a timeframe it is competing with.
bystander   A client stays connected through a storm. Measures its frames and
            how long the health endpoint takes meanwhile.
soak        Repeated storms, sampling memory, threads and outstanding work.

Every frame a probe receives is checked in full: its cursor, its timeframe, and
that neither the frame nor any panel in it claims to be live.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import psutil
import uvicorn
import websockets

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

from measure_market_ws_reconnect import (
    BACKEND,
    BuildMetrics,
    ErrorCapture,
    _free_port,
    _wait_health,
    _wait_idle,
)

SCENARIOS = ("cold", "timeframes", "bystander", "soak")
PANELS = ("xray", "strength", "leaders")
DEFAULT_TIMEFRAME = "H1"
PUSH_INTERVAL = 10.0


def _post(url: str) -> dict[str, Any]:
    with urlopen(Request(url, method="POST"), timeout=120) as response:
        return json.loads(response.read())


async def _storm_client(
    url: str, delay: float, timeframe: str | None, after_first_frame: bool
) -> str | None:
    try:
        connection = await websockets.connect(url, max_size=None, open_timeout=60)
        if timeframe:
            await connection.send(json.dumps({"type": "subscribe", "timeframe": timeframe}))
        if after_first_frame:
            await connection.recv()
        await asyncio.sleep(delay)
        connection.transport.abort()
        return None
    except Exception as exc:  # noqa: BLE001 - every failed storm connection is evidence
        return f"{type(exc).__name__}: {exc}"


async def _storm(
    url: str, clients: int, delay: float, timeframes: list[str | None],
    *, after_first_frame: bool = False,
) -> list[str]:
    errors = await asyncio.gather(*[
        _storm_client(url, delay, timeframes[i % len(timeframes)], after_first_frame)
        for i in range(clients)
    ])
    return [e for e in errors if e]


def _problems(frame: dict[str, Any], *, cursor: str | None, timeframe: str) -> list[str]:
    found = []
    if frame.get("type") != "market.frame":
        found.append(f"frame type {frame.get('type')}: {frame.get('message')}")
        return found
    if cursor and frame["cursor"] != cursor:
        found.append(f"cursor {frame['cursor']} != expected {cursor}")
    if frame["timeframe"] != timeframe or frame["xray"]["timeframe"] != timeframe:
        found.append(f"timeframe {frame['timeframe']} != expected {timeframe}")
    if frame["status"]["status"] == "LIVE" or frame["provenance"]["is_live"]:
        found.append("frame claims to be live")
    for panel in PANELS:
        if frame[panel]["provenance"]["is_live"]:
            found.append(f"{panel} claims to be live")
        if frame[panel]["as_of"] != frame["cursor"]:
            found.append(f"{panel} is at {frame[panel]['as_of']}, frame at {frame['cursor']}")
    for series in frame["xray"]["series"]:
        if any(point["ts"] > frame["cursor"] for point in series["points"]):
            found.append(f"{series['symbol']} has a point after the cursor")
    return found


async def _probe(url: str, *, cursor: str | None, timeframe: str, timeout: float) -> dict[str, Any]:
    """A fresh client's first frame, from the moment it starts to connect."""
    started = time.perf_counter()
    connection = await websockets.connect(url, max_size=None, open_timeout=timeout)
    try:
        frame = json.loads(await asyncio.wait_for(connection.recv(), timeout))
        return {
            "seconds": time.perf_counter() - started,
            "timeframe": frame.get("timeframe"),
            "status": (frame.get("status") or {}).get("status"),
            "source": (frame.get("provenance") or {}).get("source"),
            "is_live": (frame.get("provenance") or {}).get("is_live"),
            "problems": _problems(frame, cursor=cursor, timeframe=timeframe),
        }
    finally:
        await connection.close()


def _resources(server: uvicorn.Server, metrics: BuildMetrics, process: psutil.Process,
               frames: Any) -> dict[str, Any]:
    state = metrics.snapshot()
    return {
        "rss_mb": round(process.memory_info().rss / 1_048_576, 1),
        "threads": process.num_threads(),
        "connections": len(server.server_state.connections),
        "handlers": len(server.server_state.tasks),
        "builds_started": state["started"],
        "builds_active": state["active"],
        "builds_peak": state["peak_active"],
        "flights": frames.counts()["flights"] if frames else None,
        "frames_kept": frames.counts()["retained"] if frames else None,
    }


class Context:
    def __init__(self, base: str, ws: str, server, metrics, process, frames, args):
        self.base, self.ws, self.server = base, ws, server
        self.metrics, self.process, self.frames, self.args = metrics, process, frames, args
        self.bars = -1

    def move_cursor(self) -> str:
        """Somewhere nothing has been computed for. Each call is further back."""
        self.bars -= 7
        reply = _post(f"{self.base}/api/v1/market/replay/step?timeframe=M15&bars={self.bars}")
        return reply["cursor"]

    def resources(self) -> dict[str, Any]:
        return _resources(self.server, self.metrics, self.process, self.frames)

    async def idle(self) -> float:
        return await _wait_idle(self.server, self.metrics, self.args.timeout)


async def cold(ctx: Context) -> dict[str, Any]:
    await ctx.idle()
    runs = []
    for _ in range(ctx.args.repeats):
        cursor = await asyncio.to_thread(ctx.move_cursor)
        before = ctx.metrics.snapshot()["started"]
        errors = await _storm(ctx.ws, ctx.args.clients, ctx.args.abort_delay, [None])
        probe = await _probe(ctx.ws, cursor=cursor, timeframe=DEFAULT_TIMEFRAME,
                             timeout=ctx.args.timeout)
        settle = await ctx.idle()
        runs.append({
            **probe,
            "client_errors": errors,
            "settle_seconds": settle,
            "builds_for_storm": ctx.metrics.snapshot()["started"] - before,
        })
    return {"runs": runs, "worst_seconds": max(r["seconds"] for r in runs)}


async def timeframes(ctx: Context) -> dict[str, Any]:
    """The worst case. A client's first frame is always the default timeframe,
    so the storm stays for one push, which is timed from that frame: just as its
    subscribed timeframes come due it is aborted and the cursor moves, leaving
    the probe a cold build behind whatever had started."""
    await ctx.idle()
    runs = []
    every: list[str | None] = ["M5", "M15", "H1", "H4", "D1"]
    for _ in range(ctx.args.repeats):
        await asyncio.to_thread(ctx.move_cursor)
        before = ctx.metrics.snapshot()["started"]
        errors = await _storm(ctx.ws, ctx.args.clients, PUSH_INTERVAL + 0.1, every,
                              after_first_frame=True)
        cursor = await asyncio.to_thread(ctx.move_cursor)
        probe = await _probe(ctx.ws, cursor=cursor, timeframe=DEFAULT_TIMEFRAME,
                             timeout=ctx.args.timeout)
        settle = await ctx.idle()
        runs.append({
            **probe,
            "client_errors": errors,
            "settle_seconds": settle,
            "builds_for_storm": ctx.metrics.snapshot()["started"] - before,
        })
    return {"runs": runs, "worst_seconds": max(r["seconds"] for r in runs)}


async def bystander(ctx: Context) -> dict[str, Any]:
    await ctx.idle()
    health = f"{ctx.base}/api/v1/health"
    latencies: list[float] = []
    stop = asyncio.Event()

    async def poll() -> None:
        while not stop.is_set():
            started = time.perf_counter()
            await asyncio.to_thread(lambda: urlopen(health, timeout=60).read())
            latencies.append(time.perf_counter() - started)
            await asyncio.sleep(0.25)

    arrivals: list[float] = []
    problems: list[str] = []
    connection = await websockets.connect(ctx.ws, max_size=None)
    await asyncio.wait_for(connection.recv(), ctx.args.timeout)

    async def watch() -> None:
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(connection.recv(), 1.0)
            except TimeoutError:
                continue
            frame = json.loads(raw)
            arrivals.append(time.perf_counter())
            problems.extend(_problems(frame, cursor=None, timeframe=frame.get("timeframe", "")))

    poller, watcher = asyncio.create_task(poll()), asyncio.create_task(watch())
    started = time.perf_counter()
    await asyncio.to_thread(ctx.move_cursor)
    errors = await _storm(ctx.ws, ctx.args.clients, ctx.args.abort_delay, [None])
    # Long enough for the push interval to come round twice, or for a backlog
    # to show as a gap in the bystander's frames.
    await asyncio.sleep(25)
    stop.set()
    await asyncio.gather(poller, watcher)
    await connection.close()
    gaps = [b - a for a, b in zip([started, *arrivals], arrivals)]
    ordered = sorted(latencies)
    return {
        "bystander_frames": len(arrivals),
        "largest_gap_seconds": max(gaps) if gaps else None,
        "problems": problems,
        "client_errors": errors,
        "health_samples": len(ordered),
        "health_p50_seconds": ordered[len(ordered) // 2],
        "health_max_seconds": ordered[-1],
    }


async def soak(ctx: Context) -> dict[str, Any]:
    await ctx.idle()
    samples = [{"storm": 0, **ctx.resources()}]
    errors: list[str] = []
    every: list[str | None] = ["M5", "M15", "H1", "H4", "D1"]
    for storm in range(1, ctx.args.soak_storms + 1):
        await asyncio.to_thread(ctx.move_cursor)
        errors += await _storm(ctx.ws, ctx.args.clients, ctx.args.abort_delay, every)
        during = ctx.resources()
        settle = await ctx.idle()
        samples.append({"storm": storm, "settle_seconds": settle, "during": during,
                        **ctx.resources()})
    return {
        "client_errors": errors,
        "samples": samples,
        "rss_growth_mb": round(samples[-1]["rss_mb"] - samples[0]["rss_mb"], 1),
        "rss_growth_second_half_mb": round(
            samples[-1]["rss_mb"] - samples[len(samples) // 2]["rss_mb"], 1
        ),
        "thread_growth": samples[-1]["threads"] - samples[0]["threads"],
    }


async def _run(ctx: Context, chosen: list[str]) -> dict[str, Any]:
    results = {}
    for name in chosen:
        print(f"-- {name}", flush=True)
        started = time.perf_counter()
        results[name] = await globals()[name](ctx)
        results[name]["scenario_seconds"] = round(time.perf_counter() - started, 1)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8782)
    parser.add_argument("--clients", type=int, default=120)
    parser.add_argument("--abort-delay", type=float, default=0.02)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--soak-storms", type=int, default=20)
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=list(SCENARIOS))
    parser.add_argument("--target", type=float, default=5.0)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    _free_port(args.port)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("TARS_HOME", str(args.report.parent / "home"))
    os.environ.setdefault("USE_MOCK_TRADING_EVENTS", "false")
    os.environ.setdefault("BACKEND_PORT", str(args.port))
    sys.path.insert(0, str(BACKEND))

    import logging

    logging.disable(logging.INFO)
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
    errors = ErrorCapture()
    errors.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(errors)

    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=args.port, log_config=None, ws="websockets"
    ))
    thread = threading.Thread(target=server.run, name="market-ws-storm-server", daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{args.port}"
    try:
        _wait_health(f"{base}/api/v1/health")
        market_health = _wait_health(f"{base}/api/v1/market/health")
        ctx = Context(
            base, f"ws://127.0.0.1:{args.port}/api/v1/market/ws", server, metrics,
            psutil.Process(), getattr(market_xray, "_frames", None), args,
        )
        results = asyncio.run(_run(ctx, args.scenarios))
        final_health = _wait_health(f"{base}/api/v1/market/health")
        report = {
            "storm_clients": args.clients,
            "trading_execution": final_health.get("trading_execution"),
            "trading_execution_at_start": market_health.get("trading_execution"),
            "builds": metrics.snapshot(),
            "server_errors": errors.records,
            **results,
        }
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")

        worst = [results[s]["worst_seconds"] for s in ("cold", "timeframes") if s in results]
        problems = [
            p for s in results.values()
            for p in [*s.get("problems", []), *[q for r in s.get("runs", []) for q in r["problems"]]]
        ]
        print(json.dumps({
            "worst_first_frame_seconds": max(worst) if worst else None,
            "peak_active_builds": metrics.snapshot()["peak_active"],
            "builds_started": metrics.snapshot()["started"],
            "frame_problems": len(problems),
            "server_errors": len(errors.records),
            "trading_execution": final_health.get("trading_execution"),
            "report": str(args.report),
        }, indent=2))
        failed = problems or errors.records or (worst and max(worst) > args.target)
        return 1 if failed else 0
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        logging.getLogger().removeHandler(errors)


if __name__ == "__main__":
    raise SystemExit(main())
