"""Independent black-box checks for the integrated Market Intelligence API.

This module deliberately imports no TARS backend implementation.  It treats
the running service as an external product and verifies identity, replay
isolation, provenance, coverage warnings, port ownership and the permanent
trading-execution guard through HTTP responses and OS-observable behaviour.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

EXPECTED_SERVICE = "tars-backend"
EXPECTED_CONTRACT = 1
EXPECTED_THIN = {"NZD", "CHF"}
EXPECTED_BACKING = "RECORDED_CSV"


@dataclass
class Report:
    checks: list[dict[str, object]] = field(default_factory=list)

    def check(self, name: str, ok: bool, evidence: str) -> None:
        self.checks.append({"name": name, "ok": ok, "evidence": evidence})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}\n         {evidence}")

    @property
    def failed(self) -> list[str]:
        return [str(item["name"]) for item in self.checks if not item["ok"]]


def request_json(base: str, method: str, path: str, timeout: float = 60.0) -> dict:
    request = urllib.request.Request(base.rstrip("/") + path, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def get(base: str, path: str) -> dict:
    return request_json(base, "GET", path)


def post(base: str, path: str) -> dict:
    return request_json(base, "POST", path)


def provenance_errors(name: str, value: object) -> list[str]:
    if not isinstance(value, dict):
        return [f"{name}: provenance is not an object"]
    errors: list[str] = []
    expected = {
        "source": "RECORDED_STORE",
        "provider": "parquet_store",
        "replay": True,
        "is_live": False,
    }
    for key, wanted in expected.items():
        if value.get(key) != wanted:
            errors.append(f"{name}.{key}={value.get(key)!r}, expected {wanted!r}")
    if not value.get("observed_at"):
        errors.append(f"{name}.observed_at is empty")
    return errors


def identity_is_well_formed(identity: object) -> tuple[bool, str]:
    if not isinstance(identity, dict):
        return False, "identity is not an object"
    required = {
        "service",
        "contract_version",
        "instance_id",
        "build_id",
        "build_source",
        "app_version",
        "started_at",
    }
    missing = sorted(required - identity.keys())
    instance = str(identity.get("instance_id", ""))
    build = str(identity.get("build_id", ""))
    ok = (
        not missing
        and bool(re.fullmatch(r"[0-9a-f]{16}", instance))
        and bool(build)
        and identity.get("build_source") in {"git", "env", "version"}
    )
    return ok, f"missing={missing}; identity={identity}"


def probe_port_conflict(port: int) -> tuple[bool, str]:
    """A second listener must fail without disturbing the established owner."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        try:
            _stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=5)
            return False, "second listener unexpectedly stayed alive for 5 seconds"
        return proc.returncode != 0, (
            f"second listener exit={proc.returncode}; stderr={stderr.strip()[-500:]}"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def run(base: str, port: int, check_port_conflict: bool) -> Report:
    report = Report()
    health_1 = get(base, "/api/v1/health")
    health_2 = get(base, "/api/v1/health")
    identity_1 = health_1.get("identity")
    identity_2 = health_2.get("identity")

    formed, identity_evidence = identity_is_well_formed(identity_1)
    report.check("Health identity is complete and well formed", formed, identity_evidence)
    report.check(
        "Backend identity is stable within one process",
        identity_1 == identity_2,
        f"first={identity_1}; second={identity_2}",
    )
    compatible = bool(
        isinstance(identity_1, dict)
        and identity_1.get("service") == EXPECTED_SERVICE
        and identity_1.get("contract_version") == EXPECTED_CONTRACT
    )
    report.check(
        "Backend advertises the shell adoption contract",
        compatible,
        f"service={identity_1.get('service') if isinstance(identity_1, dict) else None}; "
        f"contract_version={identity_1.get('contract_version') if isinstance(identity_1, dict) else None}",
    )
    report.check(
        "Trading execution remains disabled",
        health_1.get("live_trading_enabled") is False,
        f"live_trading_enabled={health_1.get('live_trading_enabled')!r}",
    )

    if check_port_conflict:
        conflict_ok, conflict_evidence = probe_port_conflict(port)
        identity_after_conflict = get(base, "/api/v1/health").get("identity")
        report.check("A second process cannot steal the backend port", conflict_ok, conflict_evidence)
        report.check(
            "Port-conflict attempt leaves the original backend authoritative",
            identity_after_conflict == identity_1,
            f"before={identity_1}; after={identity_after_conflict}",
        )

    market_health = get(base, "/api/v1/market/health")
    status = market_health.get("status", {})
    report.check(
        "Market health is recorded replay, never live",
        status.get("status") == "REPLAY"
        and status.get("replay") is True
        and status.get("source") == "RECORDED_STORE",
        f"status={status}",
    )
    report.check(
        "Market API repeats the execution guard",
        market_health.get("trading_execution") == "DISABLED",
        f"trading_execution={market_health.get('trading_execution')!r}",
    )

    instruments = get(base, "/api/v1/market/instruments").get("instruments", [])
    missing_backing = [
        item.get("symbol")
        for item in instruments
        if not item.get("backing") or item.get("backing") != EXPECTED_BACKING
    ]
    report.check(
        "Every offered instrument declares recorded bid/ask backing",
        bool(instruments) and not missing_backing,
        f"instruments={len(instruments)}; wrong_or_missing_backing={missing_backing}",
    )

    strength = get(base, "/api/v1/market/strength")
    thin = {
        str(item.get("currency"))
        for item in strength.get("ranking", [])
        if item.get("thin_coverage") is True
    }
    loo_missing = [
        item.get("currency")
        for item in strength.get("ranking", [])
        if "leave_one_out_max_delta" not in item or "leave_one_out_driver" not in item
    ]
    report.check(
        "Thin-coverage warning remains true for exactly NZD and CHF",
        thin == EXPECTED_THIN,
        f"thin={sorted(thin)}",
    )
    report.check(
        "Every currency carries leave-one-pair-out sensitivity",
        len(strength.get("ranking", [])) == 8 and not loo_missing,
        f"currencies={len(strength.get('ranking', []))}; missing={loo_missing}",
    )

    reset = post(base, "/api/v1/market/replay/reset")
    cursor = str(reset.get("cursor"))
    # Rewind one H1 day so the replay pin is observably set rather than only
    # seeking to the live edge.
    rewind = post(base, "/api/v1/market/replay/step?timeframe=H1&bars=-24")
    replay_cursor = str(rewind.get("cursor"))
    dashboard = get(base, "/api/v1/market/dashboard?timeframe=H1&symbol=EURUSD")
    report.check(
        "Rewinding pins replay and cannot produce LIVE status",
        dashboard.get("replay_pinned") is True
        and dashboard.get("status", {}).get("status") == "REPLAY"
        and dashboard.get("status", {}).get("replay") is True,
        f"cursor={replay_cursor}; replay_pinned={dashboard.get('replay_pinned')}; "
        f"status={dashboard.get('status')}",
    )

    provenance_payloads = {
        "dashboard": dashboard.get("provenance"),
        "xray": dashboard.get("xray", {}).get("provenance"),
        "strength": dashboard.get("strength", {}).get("provenance"),
        "leaders": dashboard.get("leaders", {}).get("provenance"),
        "chart": dashboard.get("chart", {}).get("provenance"),
        "order_blocks": dashboard.get("order_blocks", {}).get("provenance"),
        "events": dashboard.get("events", {}).get("provenance"),
    }
    provenance_failures = [
        error
        for name, value in provenance_payloads.items()
        for error in provenance_errors(name, value)
    ]
    report.check(
        "Every dashboard observation carries non-live recorded provenance",
        not provenance_failures,
        f"observations={len(provenance_payloads)}; errors={provenance_failures}",
    )

    repeated = post(base, f"/api/v1/market/replay/seek?to={urllib.parse.quote(replay_cursor, safe='')}")
    repeated_dashboard = get(base, "/api/v1/market/dashboard?timeframe=H1&symbol=EURUSD")
    signatures = (
        dashboard.get("xray"),
        dashboard.get("strength"),
        dashboard.get("chart"),
    )
    repeated_signatures = (
        repeated_dashboard.get("xray"),
        repeated_dashboard.get("strength"),
        repeated_dashboard.get("chart"),
    )
    report.check(
        "The same replay cursor reproduces deterministic market observations",
        repeated.get("cursor") == replay_cursor and signatures == repeated_signatures,
        f"cursor={repeated.get('cursor')}; xray/strength/chart identical={signatures == repeated_signatures}",
    )

    # Leave the shared service at its newest recorded bar even when a check
    # above failed, so verification does not surprise the next user session.
    final_reset = post(base, "/api/v1/market/replay/reset")
    report.check(
        "Replay reset restores the newest recorded cursor",
        final_reset.get("cursor") == cursor and final_reset.get("status", {}).get("status") != "LIVE",
        f"initial={cursor}; final={final_reset}",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--skip-port-conflict", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/market-integration/integration-verification.json"),
    )
    args = parser.parse_args()

    print("==== TARS Market Intelligence — independent integration verification ====\n")
    try:
        report = run(args.api, args.port, not args.skip_port_conflict)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(f"  [FAIL] verifier could not complete: {type(exc).__name__}: {exc}")
        return 1
    finally:
        # Replay is global backend state. Even an interrupted assertion must
        # not leave the next desktop session pinned in the past.
        try:
            post(args.api, "/api/v1/market/replay/reset")
        except Exception as exc:  # noqa: BLE001 - cleanup is best-effort diagnostics
            print(f"  [WARN] could not reset replay after verification: {exc}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"checks": report.checks}, indent=2), encoding="utf-8")
    passed = len(report.checks) - len(report.failed)
    print(f"\n==== {passed}/{len(report.checks)} integration checks passed ====")
    print(f"evidence -> {args.output.resolve()}")
    if report.failed:
        print("FAILED: " + ", ".join(report.failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
