"""Chart-analysis latency benchmark — TARS Alexa-Speed Phase H.

A real, runnable acceptance harness for the "analyze the chart" warm/cold
paths, driven against an actual running backend (not mocked) with a real
chart image supplied by the operator. Reports P50/P90/P95/max plus the
Part 25 quality checklist (symbol/timeframe present when structured, no
fabricated confidence, disclaimer present) -- a fast-but-wrong or
fast-but-fabricated response is a failed run, never just a fast one.

This intentionally does NOT try to capture a chart itself -- that needs
either the real Tauri app (native Windows.Graphics.Capture / BitBlt) or a
platform-specific helper outside this backend's scope. Point it at a PNG/
BMP file of a real chart (e.g. one written by capture_wgc.rs's own
`#[ignore]`d live-capture test, or any real capture the operator already
has) and it drives real HTTP requests against a real backend from there.

Usage:
    python scripts/chart_latency_benchmark.py cold --image chart.png --runs 20
    python scripts/chart_latency_benchmark.py warm --image chart.png \
        --window-id 3737038 --runs 20

Requires `httpx` (already a pinned dependency, see requirements.txt) and a
backend already running and reachable at --backend-url (default
http://127.0.0.1:8000). Does not spawn or manage the backend process itself.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

# Part 24's acceptance bar: at least this fraction of warm runs must begin
# a meaningful response within approximately 5 seconds.
WARM_TARGET_SECONDS = 5.0
WARM_ACCEPTANCE_FRACTION = 18 / 20


@dataclass
class RunResult:
    run: int
    total_s: float | None
    warm_path: bool | None
    quality_failures: list[str] = field(default_factory=list)
    error: str | None = None


def _guess_image_format(path: Path) -> str:
    suffix = path.suffix.lower()
    return {"png": "image/png", "bmp": "image/bmp", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(
        suffix.lstrip("."), "image/png"
    )


def _quality_check(result: dict[str, Any]) -> list[str]:
    """Part 25's checklist, applied to whatever the endpoint actually
    returned -- failures are collected, not raised, so a benchmark run
    reports exactly what went wrong rather than aborting on the first
    issue."""
    failures: list[str] = []
    disclaimer = (result.get("disclaimer") or "").lower()
    if "quant_brain" not in disclaimer:
        failures.append("missing quant_brain disclaimer")

    speech = (result.get("speech_text") or "") + (result.get("formatted_tars_text") or "")
    if "%" in speech and "confidence" in speech.lower():
        failures.append("possible fabricated confidence percentage in spoken/text output")
    for guaranteed_word in ("guaranteed", "certain to", "will definitely"):
        if guaranteed_word in speech.lower():
            failures.append(f"unsupported certainty language: '{guaranteed_word}'")

    if result.get("structured") and not (result.get("instrument") or result.get("timeframe")):
        failures.append("structured=True but no instrument/timeframe parsed")

    return failures


def _post_chart_watch_frame(backend_url: str, *, window_id: str, image_bytes: bytes, image_format: str) -> dict:
    resp = httpx.post(
        f"{backend_url}/api/v1/chart-watch/frame",
        json={
            "chart_window_id": window_id,
            "image_data_base64": base64.b64encode(image_bytes).decode("ascii"),
            "image_format": image_format,
            "trigger_reason": "benchmark_warmup",
        },
        # Matches app.config.Settings.chart_analysis_timeout_seconds
        # (120.0) -- a real vision call was observed exceeding 60s once
        # during this tool's own development, so 60s is not a safe margin.
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


def _run_analyze_chart(
    backend_url: str, *, image_bytes: bytes, image_format: str, window_id: str | None, run: int
) -> RunResult:
    payload: dict[str, Any] = {
        "conversation_id": f"benchmark-{run}",
        "capture": {
            "image_data_base64": base64.b64encode(image_bytes).decode("ascii"),
            "image_format": image_format,
        },
    }
    if window_id:
        payload["capture"]["window_id"] = window_id

    t0 = time.perf_counter()
    try:
        with httpx.stream(
            "POST", f"{backend_url}/api/v1/assistant/analyze-chart/stream", json=payload, timeout=120
        ) as resp:
            resp.raise_for_status()
            complete: dict[str, Any] | None = None
            for line in resp.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                event = json.loads(line[len("data: "):])
                if event.get("type") == "complete":
                    complete = event
                elif event.get("type") == "error":
                    return RunResult(run=run, total_s=None, warm_path=None, error=event.get("detail"))
        total_s = time.perf_counter() - t0
    except httpx.HTTPError as exc:
        return RunResult(run=run, total_s=None, warm_path=None, error=str(exc))

    if complete is None:
        return RunResult(run=run, total_s=total_s, warm_path=None, error="no 'complete' event received")

    warm_path = bool((complete.get("timing") or {}).get("warm_path"))
    failures = _quality_check(complete.get("result") or {})
    return RunResult(run=run, total_s=total_s, warm_path=warm_path, quality_failures=failures)


def _percentile(sorted_values: list[float], pct: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100) * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    frac = rank - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * frac


def _report(results: list[RunResult], *, mode: str) -> int:
    totals = sorted(r.total_s for r in results if r.total_s is not None)
    errors = [r for r in results if r.error]
    quality_failures = [r for r in results if r.quality_failures]

    print(f"\n=== {mode.upper()} PATH RESULTS ({len(results)} runs) ===")
    print(f"errors: {len(errors)}")
    for r in errors:
        print(f"  run {r.run}: {r.error}")
    print(f"quality failures: {len(quality_failures)}")
    for r in quality_failures:
        print(f"  run {r.run}: {r.quality_failures}")

    if totals:
        print(f"P50={_percentile(totals, 50):.3f}s  P90={_percentile(totals, 90):.3f}s  "
              f"P95={_percentile(totals, 95):.3f}s  max={totals[-1]:.3f}s  min={totals[0]:.3f}s")

    if mode == "warm":
        within_target = sum(1 for t in totals if t <= WARM_TARGET_SECONDS)
        fraction = within_target / len(results) if results else 0.0
        passed = fraction >= WARM_ACCEPTANCE_FRACTION
        print(
            f"within {WARM_TARGET_SECONDS}s: {within_target}/{len(results)} "
            f"({fraction:.0%}) -- acceptance bar {WARM_ACCEPTANCE_FRACTION:.0%}: "
            f"{'PASS' if passed else 'FAIL'}"
        )
        return 0 if passed and not errors and not quality_failures else 1

    return 0 if not errors and not quality_failures else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["warm", "cold"])
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--window-id", default=None)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--backend-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    if args.mode == "warm" and not args.window_id:
        parser.error("--window-id is required for warm-mode runs (identifies the chart window to HotChartState)")

    image_bytes = args.image.read_bytes()
    image_format = _guess_image_format(args.image)

    if args.mode == "warm":
        print("warming HotChartState with one real vision call...")
        outcome = _post_chart_watch_frame(
            args.backend_url, window_id=args.window_id, image_bytes=image_bytes, image_format=image_format
        )
        print(f"warmup outcome: {outcome}")
        if outcome.get("action") not in ("refreshed", "skipped_fresh"):
            print("warmup did not produce usable state -- aborting", file=sys.stderr)
            return 2

    results: list[RunResult] = []
    for i in range(1, args.runs + 1):
        window_id = args.window_id if args.mode == "warm" else None
        r = _run_analyze_chart(
            args.backend_url, image_bytes=image_bytes, image_format=image_format, window_id=window_id, run=i
        )
        status = "OK" if not r.error else f"ERROR: {r.error}"
        print(f"run {i}/{args.runs}: total={r.total_s} warm_path={r.warm_path} {status}")
        results.append(r)

    return _report(results, mode=args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
