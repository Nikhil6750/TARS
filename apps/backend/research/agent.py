"""QuantResearchAgent: the deterministic executor. It never edits a spec, never searches parameters and never
reads the untouched test period unless the one-shot test is explicitly authorised.

ResearchSpec -> memory check -> register (freeze) -> load data (dev+validation only) -> quality gate ->
causality check -> pre-run warnings -> features -> simulate (real bid/ask + costs) -> dev/validation metrics
-> regimes -> sensitivity -> warnings -> rejection criteria -> persisted report + evidence packet."""
from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Callable

import pandas as pd

from .antioverfit import ResearchWarning, multiple_testing, post_run_warnings, pre_run_warnings
from .backtest import simulate
from .features import compute_features, verify_causality
from .memory import ResearchMemory
from .metrics import clean, compute_metrics, regime_breakdowns
from .quality import DataQualityReport, validate_bars
from .registry import ExperimentRegistry, RegistryError
from .schema import TIMEFRAME_MINUTES, derive_timeframe
from .spec import ResearchSpec
from .storage import FinalDataAccessDenied, MarketStore  # noqa: F401 (re-exported)

WARMUP_BARS = 600
CLASSIFICATION = {"PRELIMINARY": "PRELIMINARY_EVIDENCE", "VALIDATING": "PRELIMINARY_EVIDENCE",
                  "VALIDATED": "VALIDATED_FINDING", "REJECTED": "FAILED_EXPERIMENT", "FAILED": "FAILED_EXPERIMENT",
                  "PROPOSED": "HYPOTHESIS", "RUNNING": "HYPOTHESIS"}


class ResearchRejected(RuntimeError):
    """The proposal was refused before registration (research memory)."""


class QuantResearchAgent:
    def __init__(self, registry: ExperimentRegistry, store: MarketStore, reports_dir: Path,
                 memory: ResearchMemory | None = None) -> None:
        self.registry, self.store = registry, store
        self.reports_dir = Path(reports_dir)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.memory = memory or ResearchMemory(registry)
        self._gaps: dict[str, list] = {}
        self.excluded_gap_trades = 0

    def _sim(self, inst: str, bars, spec, costs, **kw):
        """simulate() + deterministic exclusion of trades whose life overlaps an unexplained provider gap (counted, reported)."""
        t = simulate(bars, spec, costs, **kw)
        gaps = self._gaps.get(inst) or []
        if gaps and len(t):
            hit = pd.Series(False, index=t.index)
            for a, b in gaps:
                hit |= (t["entry_time"] <= b) & (t["exit_time"] >= a)
            self.excluded_gap_trades += int(hit.sum())
            t = t[~hit].reset_index(drop=True)
        return t

    # ---- intake ---------------------------------------------------------------------------------
    def submit(self, spec: ResearchSpec) -> str:
        verdict = self.memory.check(spec)
        if not verdict.allowed:
            raise ResearchRejected(verdict.reason)
        return self.registry.register(spec)

    # ---- data -----------------------------------------------------------------------------------
    def _load(self, spec: ResearchSpec, inst: str, upto: pd.Timestamp, *, final_ok: bool = False) -> tuple[pd.DataFrame, pd.Timestamp]:
        if upto > pd.Timestamp(spec.validation.end).tz_convert("UTC") and not final_ok:
            raise FinalDataAccessDenied("loading data beyond the validation period is reserved for explicit final testing")
        tf_min = TIMEFRAME_MINUTES[spec.data.timeframe]
        warm_start = pd.Timestamp(spec.development.start).tz_convert("UTC") - pd.Timedelta(minutes=tf_min * (WARMUP_BARS + spec.embargo_bars))
        bars = self.store.read_bars(spec.data.provider, inst, spec.data.source_timeframe, warm_start, upto,
                                    authorized_final=final_ok, purpose="untouched_test")
        return bars, warm_start

    def _prepare(self, spec: ResearchSpec, upto: pd.Timestamp, *, final_ok: bool = False, on_final_load=None
                 ) -> tuple[dict[str, pd.DataFrame], dict[str, DataQualityReport], str]:
        derived, reports, versions = {}, {}, []
        for inst in spec.instruments:
            if final_ok and on_final_load is not None:
                on_final_load()  # record the FIRST access before any final row is read
            bars, _ = self._load(spec, inst, upto, final_ok=final_ok)
            years = max((bars["timestamp_utc"].iloc[-1] - bars["timestamp_utc"].iloc[0]).days / 365.25, 0.0) if len(bars) > 1 else 0.0
            # a few isolated outages per year are normal for a retail feed (listed, trades spanning them excluded); more is systemic
            rep = validate_bars(bars, timeframe=spec.data.source_timeframe, max_large_gaps=int(years * 5))
            reports[inst] = rep
            self._gaps[inst] = [(pd.Timestamp(a), pd.Timestamp(b)) for a, b, _ in rep.provider_gaps]
            if rep.blocks_experiment:
                continue
            versions.append(self.store.data_version(bars))
            derived[inst] = derive_timeframe(bars, spec.data.timeframe, drop_partial_tail=True).reset_index(drop=True)
        return derived, reports, "+".join(versions)

    # ---- main run (development + validation only) ---------------------------------------------------
    def run(self, hid: str) -> dict:
        spec = self.registry.spec(hid)
        rec = self.registry.get(hid)
        if spec.spec_hash() != rec["spec_hash"]:
            raise RegistryError(f"{hid}: stored spec no longer matches its frozen hash")
        self.registry.transition(hid, "RUNNING")
        try:
            return self._run(hid, spec)
        except Exception as exc:  # persisted, never lost
            self.registry.record(hid, failure_reason=f"AGENT_ERROR: {type(exc).__name__}: {exc}",
                                 warnings_json=[{"code": "AGENT_ERROR", "message": traceback.format_exc()[-1500:], "blocking": True}])
            self.registry.transition(hid, "FAILED", f"{type(exc).__name__}: {exc}")
            return self._finish(hid, spec, {}, {})

    def _fail(self, hid: str, spec: ResearchSpec, reason: str, results: dict, extra: dict) -> dict:
        self.registry.record(hid, failure_reason=reason)
        self.registry.transition(hid, "FAILED", reason)
        return self._finish(hid, spec, results, extra)

    def _run(self, hid: str, spec: ResearchSpec) -> dict:
        val_end = pd.Timestamp(spec.validation.end).tz_convert("UTC")
        derived, reports, dver = self._prepare(spec, val_end)  # untouched test period is NOT loaded
        qjson = {i: r.to_dict() for i, r in reports.items()}
        self.registry.record(hid, quality_json=qjson, data_version=dver or None)
        blocked = {i: sorted(r.codes("BLOCK")) for i, r in reports.items() if r.blocks_experiment}
        if blocked:
            return self._fail(hid, spec, f"DATA_QUALITY_BLOCK: {blocked}", {}, {})

        too_short = [i for i, b in derived.items() if len(b) < spec.data.min_bars_per_period]
        if too_short:
            return self._fail(hid, spec, f"INSUFFICIENT_DATA: {too_short} below min_bars_per_period", {}, {})

        lookahead = sorted({f for b in derived.values() for f in verify_causality(b.iloc[:4000].reset_index(drop=True), spec.features)})
        n_family = self.registry.trials_in_family(spec.instruments, exclude=hid)
        pre = pre_run_warnings(spec, n_family_trials=n_family, lookahead_features=lookahead)
        conflicts = self.registry.final_access_conflicts(spec, exclude=hid)
        if conflicts:
            pre.append(ResearchWarning("FINAL_PERIOD_NOT_UNTOUCHED", "the final period was already loaded by "
                       + ", ".join(c["experiment_id"] for c in conflicts) + "; it can no longer serve as untouched data for this spec"))
        if any(w.blocking for w in pre):
            return self._fail(hid, spec, "BLOCKING_WARNING: " + "; ".join(w.code for w in pre if w.blocking), {},
                              {"warnings": pre})

        d0, d1 = (pd.Timestamp(spec.development.start).tz_convert("UTC"), pd.Timestamp(spec.development.end).tz_convert("UTC"))
        v0, v1 = (pd.Timestamp(spec.validation.start).tz_convert("UTC"), val_end)
        dev_t, val_t = [], []
        self.excluded_gap_trades = 0
        stress = {"dev": {}, "val": {}}
        feats_by_inst = {i: compute_features(b, spec.features) for i, b in derived.items()}
        for inst, bars in derived.items():
            f = feats_by_inst[inst]
            for label, (a, b, emb), sink in (("dev", (d0, d1, 0), dev_t), ("val", (v0, v1, spec.embargo_bars), val_t)):
                t = self._sim(inst, bars, spec, spec.costs, start=a, end=b, feats=f, embargo_bars=emb)
                t["instrument"] = inst
                sink.append(t)
        primary_excluded = self.excluded_gap_trades   # primary dev+validation run only (stress re-runs excluded)
        dev_trades = pd.concat(dev_t, ignore_index=True) if dev_t else pd.DataFrame()
        val_trades = pd.concat(val_t, ignore_index=True) if val_t else pd.DataFrame()
        dev_m, val_m = compute_metrics(dev_trades), compute_metrics(val_trades)

        def stressed(costs, start, end, emb):
            out = []
            for inst, bars in derived.items():
                t = self._sim(inst, bars, spec, costs, start=start, end=end, feats=feats_by_inst[inst], embargo_bars=emb)
                out.append(t)
            return compute_metrics(pd.concat(out, ignore_index=True))
        sens = {}
        for mult in spec.sensitivity.spread_multipliers:
            c = spec.costs.with_stress(spread_mult=mult)
            sens[f"spread_x{mult:g}".replace(".0", "")] = {"development": stressed(c, d0, d1, 0), "validation": stressed(c, v0, v1, spec.embargo_bars)}
        for slip in spec.sensitivity.slippage_per_side:
            c = spec.costs.with_stress(slippage_per_side=slip)
            sens[f"slippage_{slip:g}"] = {"development": stressed(c, d0, d1, 0), "validation": stressed(c, v0, v1, spec.embargo_bars)}
        stress_val = {k: v["validation"] for k, v in sens.items()}

        neighbours = []
        for var in spec.sensitivity.parameter_variants:  # reported for stability only; never used to select parameters
            vs = spec.model_copy(update={"features": [f.model_copy(update={"n": var.get(f.name, f.n)}) for f in spec.features]})
            tv = []
            for inst, bars in derived.items():
                tv.append(self._sim(inst, bars, vs, spec.costs, start=v0, end=v1, feats=compute_features(bars, vs.features), embargo_bars=spec.embargo_bars))
            m = compute_metrics(pd.concat(tv, ignore_index=True))
            neighbours.append({"variant": var, "trades": m.get("trades", 0), "net_expectancy_r": m.get("net_expectancy_r")})

        regimes = {"development": regime_breakdowns(dev_trades, spec.regime_tests) if len(dev_trades) else {},
                   "validation": regime_breakdowns(val_trades, spec.regime_tests) if len(val_trades) else {}}
        by_inst = {i: compute_metrics(val_trades[val_trades["instrument"] == i]) for i in spec.instruments} if len(val_trades) else {}
        post = post_run_warnings(spec, dev=dev_m, val=val_m, val_trades=val_trades, dev_trades=dev_trades,
                                 stress=stress_val, neighbours=neighbours, n_family_trials=n_family)
        warns = [*pre, *post]
        checks = self._criteria(spec, dev_m, val_m, sens, warns)
        results = {"development": dev_m, "validation": val_m, "sensitivity": sens, "neighbours": neighbours,
                   "per_instrument_validation": by_inst, "criteria": checks,
                   "multiple_testing": multiple_testing(n_family + 1), "cost_model": spec.costs.model_dump(),
                   "periods": {k: [str(getattr(spec, p).start), str(getattr(spec, p).end)] for k, p in
                               (("development", "development"), ("validation", "validation"), ("untouched_test", "untouched_test"))},
                   "untouched_test": "NOT_EVALUATED",
                   "provider_gap_exclusions": {"unexplained_gaps": {i: len(g) for i, g in self._gaps.items()},
                                               "trades_excluded_spanning_gaps": primary_excluded}}
        failed = [k for k, v in checks.items() if not v["pass"]]
        self.registry.record(hid, results_json=clean(results), regime_results_json=clean(regimes),
                             warnings_json=[w.to_dict() for w in warns])
        if failed:
            reason = "REJECTION_CRITERIA: " + ", ".join(failed)
            self.registry.record(hid, failure_reason=reason)
            self.registry.transition(hid, "REJECTED", reason)
        else:
            self.registry.transition(hid, "PRELIMINARY", "development+validation criteria met; untouched test pending")
        return self._finish(hid, spec, results, {"regimes": regimes, "warnings": warns})

    @staticmethod
    def _criteria(spec: ResearchSpec, dev: dict, val: dict, sens: dict, warns: list[ResearchWarning]) -> dict:
        rc = spec.rejection
        out = {}
        out["min_trades_development"] = {"pass": dev.get("trades", 0) >= rc.min_trades_per_period, "value": dev.get("trades", 0)}
        out["min_trades_validation"] = {"pass": val.get("trades", 0) >= rc.min_trades_per_period, "value": val.get("trades", 0)}
        v = val.get("net_expectancy_r")
        out["validation_net_expectancy"] = {"pass": v is not None and v > rc.min_net_expectancy_r, "value": v}
        d = dev.get("net_expectancy_r")
        out["oos_degradation"] = {"pass": not (d is not None and v is not None and d > 0 and v < d * (1 - rc.max_validation_degradation)),
                                  "value": {"dev": d, "val": v}}
        out["max_drawdown"] = {"pass": val.get("max_drawdown_r", 0.0) <= rc.max_drawdown_r, "value": val.get("max_drawdown_r")}
        if rc.require_positive_after_2x_spread:
            s2 = sens.get("spread_x2", {}).get("validation", {}).get("net_expectancy_r")
            out["positive_after_2x_spread"] = {"pass": s2 is not None and s2 > 0, "value": s2}
        out["no_blocking_warnings"] = {"pass": not any(w.blocking for w in warns), "value": [w.code for w in warns if w.blocking]}
        return out

    # ---- one-shot untouched test ----------------------------------------------------------------------
    def run_untouched_test(self, hid: str, *, authorized: bool) -> dict:
        """Evaluate the untouched final period exactly once. Requires PRELIMINARY status + explicit authorisation."""
        if not authorized:
            raise PermissionError("untouched test requires explicit authorisation")
        spec = self.registry.spec(hid)
        rec = self.registry.get(hid)
        if rec["status"] != "PRELIMINARY":
            raise RegistryError(f"{hid} is {rec['status']}; only PRELIMINARY experiments may face the untouched test")
        t0, t1 = (pd.Timestamp(spec.untouched_test.start).tz_convert("UTC"), pd.Timestamp(spec.untouched_test.end).tz_convert("UTC"))
        conflicts = self.registry.final_access_conflicts(spec, exclude=hid)
        if conflicts:  # refuse BEFORE touching the data: it is no longer untouched
            raise RegistryError(f"final period no longer untouched: already accessed by {[c['experiment_id'] for c in conflicts]}")
        derived, reports, dver = self._prepare(spec, t1, final_ok=True, on_final_load=lambda: self.registry.mark_final_access(hid))
        blocked = {i: sorted(r.codes("BLOCK")) for i, r in reports.items() if r.blocks_experiment}
        if blocked:  # refuse BEFORE burning the one-shot test
            raise RegistryError(f"test-period data quality blocks the test (not consumed): {blocked}")
        self.registry.consume_test(hid)
        self.registry.transition(hid, "VALIDATING", "untouched test authorised")
        trades = []
        for inst, bars in derived.items():
            t = self._sim(inst, bars, spec, spec.costs, start=t0, end=t1, feats=compute_features(bars, spec.features), embargo_bars=spec.embargo_bars)
            t["instrument"] = inst
            trades.append(t)
        tr = pd.concat(trades, ignore_index=True)
        m = compute_metrics(tr)
        prior = rec["results"]
        val_ref = (prior["validation"].get("net_expectancy_r") if prior else None)
        rc = spec.rejection
        checks = {
            "min_trades_test": {"pass": m.get("trades", 0) >= rc.min_trades_per_period, "value": m.get("trades", 0)},
            "test_net_expectancy": {"pass": (m.get("net_expectancy_r") or 0) > rc.min_net_expectancy_r, "value": m.get("net_expectancy_r")},
            "test_degradation_vs_validation": {"pass": not (val_ref and val_ref > 0 and (m.get("net_expectancy_r") or 0) < val_ref * (1 - rc.max_validation_degradation)),
                                               "value": {"validation": val_ref, "test": m.get("net_expectancy_r")}},
            "test_max_drawdown": {"pass": m.get("max_drawdown_r", 0.0) <= rc.max_drawdown_r, "value": m.get("max_drawdown_r")},
        }
        results = {**(prior or {}), "untouched_test": {"metrics": m, "criteria": checks,
                   "regimes": regime_breakdowns(tr, spec.regime_tests) if len(tr) else {}}}
        self.registry.record(hid, results_json=clean(results))
        failed = [k for k, v in checks.items() if not v["pass"]]
        if failed:
            reason = "UNTOUCHED_TEST_FAILED: " + ", ".join(failed)
            self.registry.record(hid, failure_reason=reason)
            self.registry.transition(hid, "REJECTED", reason)
        else:
            self.registry.transition(hid, "VALIDATED", "untouched test criteria met (research status only; not a strategy)")
        return self._finish(hid, spec, results, {})

    # ---- reporting ------------------------------------------------------------------------------------
    def _finish(self, hid: str, spec: ResearchSpec, results: dict, extra: dict) -> dict:
        packet = self.evidence_packet(hid)
        (self.reports_dir / f"{hid}.json").write_text(json.dumps(packet, indent=2, default=str), encoding="utf-8")
        (self.reports_dir / f"{hid}.md").write_text(render_markdown(packet), encoding="utf-8")
        return packet

    def evidence_packet(self, hid: str, max_chars: int = 12000) -> dict:
        """Compact, Claude-ready evidence: summary statistics only, never raw bars or full trade lists."""
        r = self.registry.get(hid)
        spec = self.registry.spec(hid)
        res = r["results"] or {}
        pkt = {
            "experiment_id": hid, "title": spec.title, "status": r["status"], "classification": CLASSIFICATION[r["status"]],
            "diagnostic": spec.diagnostic, "claimed_edge": False,
            "hypothesis": spec.hypothesis, "mechanism": r["mechanism"], "instruments": spec.instruments,
            "timeframe": spec.data.timeframe, "data_version": r["data_version"], "code_version": r["git_version"],
            "spec_hash": r["spec_hash"], "cost_assumptions": r["cost_assumptions"],
            "data_quality": {i: {"passed": q["passed"], "issues": [(x["code"], x["severity"], x["count"]) for x in q["issues"]]}
                             for i, q in (r["quality"] or {}).items()},
            "development": _slim(res.get("development")), "validation": _slim(res.get("validation")),
            "untouched_test": res.get("untouched_test") if isinstance(res.get("untouched_test"), str) else
                              _slim((res.get("untouched_test") or {}).get("metrics")),
            "sensitivity": {k: {p: _slim(m) for p, m in v.items()} for k, v in (res.get("sensitivity") or {}).items()},
            "parameter_neighbours": res.get("neighbours"), "criteria": res.get("criteria"),
            "multiple_testing": res.get("multiple_testing"), "regimes": r["regime_results"],
            "warnings": r["warnings"], "failure_reason": r["failure_reason"],
            "final_data": {"first_accessed_at": r.get("final_first_accessed_at"), "untouched": not r.get("final_first_accessed_at")},
            "diagnostic_kind": spec.diagnostic_kind, "cost_notes": _cost_notes(spec),
            "allowed_conclusions": "Only a status of VALIDATED permits VALIDATED_FINDING; a profitable backtest alone never does.",
        }
        text = json.dumps(pkt, default=str)
        if len(text) > max_chars:  # drop the bulkiest optional sections first
            pkt["regimes"] = "TRUNCATED_FOR_SIZE (see report file)"
        return clean(pkt)


def _cost_notes(spec: ResearchSpec) -> dict:
    c = spec.costs
    return {"spread": "real observed bid/ask (x%g)" % c.spread_mult, "commission_round_turn": c.commission_round_turn,
            "slippage_per_side": c.slippage_per_side, "latency_bars": c.latency_bars,
            "financing": "DISABLED placeholder (intraday research)" if c.financing_per_bar == 0 else c.financing_per_bar}


def _slim(m: dict | None) -> dict | None:
    if not m:
        return m
    keep = ("trades", "net_expectancy_r", "gross_expectancy_r", "median_r", "win_rate", "avg_winner_r", "avg_loser_r",
            "profit_factor", "net_total_r", "gross_total_r", "cost_drag_r_per_trade", "max_drawdown_r", "max_drawdown_days",
            "return_over_drawdown", "tail_loss_cvar5_r", "worst_trade_r", "max_consecutive_losses", "t_stat")
    return {k: m[k] for k in keep if k in m}


def render_markdown(p: dict) -> str:
    lines = [f"# {p['experiment_id']} — {p['title']}", "",
             f"Status: **{p['status']}** · classification: **{p['classification']}** · diagnostic: {p['diagnostic']} · claimed edge: **NO**", "",
             f"Hypothesis: {p['hypothesis']}", "", "## Results (net of costs)"]
    for k in ("development", "validation", "untouched_test"):
        lines.append(f"- {k}: `{json.dumps(p.get(k), default=str)}`")
    lines += ["", "## Criteria", f"`{json.dumps(p.get('criteria'), default=str)}`", "", "## Warnings"]
    lines += [f"- [{'BLOCK' if w.get('blocking') else 'warn'}] {w['code']}: {w['message']}" for w in (p.get("warnings") or [])] or ["- none"]
    if p.get("failure_reason"):
        lines += ["", f"Failure reason: {p['failure_reason']}"]
    return "\n".join(lines) + "\n"
