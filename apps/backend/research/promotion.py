"""Strategy promotion gate. A profitable backtest NEVER creates a ValidatedStrategy.

CandidateStrategy: a researcher's proposal derived from a PRELIMINARY/VALIDATED experiment (no authority).
ValidatedStrategy: only obtainable through `promote()`, which re-checks the registry and every criterion and
needs a named human approver. Neither type can trade: live_execution_enabled is a constant False and nothing
here consults or sets LIVE_TRADING_ENABLED."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .registry import ExperimentRegistry

_TOKEN = object()


class PromotionDenied(RuntimeError):
    pass


@dataclass(frozen=True)
class CandidateStrategy:
    experiment_id: str
    spec_hash: str
    status_at_creation: str
    note: str = "candidate only: no execution authority"
    live_execution_enabled: bool = False


@dataclass(frozen=True)
class ValidatedStrategy:
    experiment_id: str
    spec_hash: str
    data_version: str
    approved_by: str
    approved_at: str
    criteria_snapshot: dict = field(default_factory=dict)
    live_execution_enabled: bool = False
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self._token is not _TOKEN:
            raise PromotionDenied("ValidatedStrategy can only be created by promote()")


def make_candidate(registry: ExperimentRegistry, hid: str) -> CandidateStrategy:
    r = registry.get(hid)
    if r["status"] not in {"PRELIMINARY", "VALIDATING", "VALIDATED"}:
        raise PromotionDenied(f"{hid} is {r['status']}; only PRELIMINARY or better can become a candidate")
    return CandidateStrategy(hid, r["spec_hash"], r["status"])


def promote(registry: ExperimentRegistry, hid: str, *, approved_by: str, min_trades_test: int = 30) -> ValidatedStrategy:
    r = registry.get(hid)
    if not approved_by or not approved_by.strip():
        raise PromotionDenied("explicit human approver required")
    if r["diagnostic"]:
        raise PromotionDenied("diagnostic experiments can never be promoted")
    if r["status"] != "VALIDATED":
        raise PromotionDenied(f"{hid} status is {r['status']}, not VALIDATED")
    if not r["test_consumed"]:
        raise PromotionDenied("untouched test was never evaluated")
    res = r["results"] or {}
    test = res.get("untouched_test")
    if not isinstance(test, dict):
        raise PromotionDenied("no untouched-test results recorded")
    failed = [k for k, v in {**(res.get("criteria") or {}), **(test.get("criteria") or {})}.items() if not v.get("pass")]
    if failed:
        raise PromotionDenied(f"failed criteria: {failed}")
    if (test.get("metrics") or {}).get("trades", 0) < min_trades_test:
        raise PromotionDenied("untouched-test sample too small")
    if any(w.get("blocking") for w in (r["warnings"] or [])):
        raise PromotionDenied("blocking research warnings present")
    if registry.spec(hid).spec_hash() != r["spec_hash"]:
        raise PromotionDenied("spec no longer matches its frozen hash")
    return ValidatedStrategy(hid, r["spec_hash"], r["data_version"] or "", approved_by.strip(),
                             datetime.now(timezone.utc).isoformat(), {"validation": res.get("validation"), "test": test.get("metrics")},
                             _token=_TOKEN)
