"""Anti-overfitting engine: ResearchWarnings, some of which block the experiment outright."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import pandas as pd

from .spec import MAX_PARAM_VARIANTS_BLOCK, ResearchSpec


@dataclass
class ResearchWarning:
    code: str
    message: str
    blocking: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _norm_ppf(p: float) -> float:
    """Acklam-free: bisection on erfc, enough accuracy for a significance threshold."""
    lo, hi = 0.0, 10.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if 0.5 * math.erfc(mid / math.sqrt(2)) > p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def multiple_testing(n_trials: int, alpha: float = 0.05) -> dict:
    """Bonferroni-style: every prior trial in the family counts. Returns the adjusted alpha / critical |t|."""
    n = max(n_trials, 1)
    adj = alpha / n
    return {"n_trials": n, "adjusted_alpha": adj, "critical_abs_t": _norm_ppf(adj / 2)}


def pre_run_warnings(spec: ResearchSpec, *, n_family_trials: int, lookahead_features: list[str]) -> list[ResearchWarning]:
    w: list[ResearchWarning] = []
    if lookahead_features:
        w.append(ResearchWarning("LOOKAHEAD", f"features change when future bars are removed: {lookahead_features}", True))
    nvar = len(spec.sensitivity.parameter_variants)
    if nvar > 9:
        w.append(ResearchWarning("TOO_MANY_PARAMETER_VARIATIONS", f"{nvar} parameter variants declared (>9); "
                                 f"hard limit {MAX_PARAM_VARIANTS_BLOCK}"))
    ncond = len(spec.signal.long_when) + len(spec.signal.short_when)
    if ncond > 6:
        w.append(ResearchWarning("EXCESSIVE_FILTERING", f"{ncond} signal conditions; each filter is a degree of freedom"))
    if len(spec.instruments) == 1:
        w.append(ResearchWarning("SINGLE_INSTRUMENT", "tested on one instrument only; cross-instrument robustness unknown"))
    if n_family_trials >= 5:
        mt = multiple_testing(n_family_trials + 1)
        w.append(ResearchWarning("MULTIPLE_TESTING_RISK", f"{n_family_trials} prior experiments on these instruments; "
                                 f"required |t| >= {mt['critical_abs_t']:.2f} (alpha {mt['adjusted_alpha']:.4f})"))
    return w


def post_run_warnings(spec: ResearchSpec, *, dev: dict, val: dict, val_trades: pd.DataFrame, dev_trades: pd.DataFrame,
                      stress: dict, neighbours: list[dict], n_family_trials: int) -> list[ResearchWarning]:
    w: list[ResearchWarning] = []
    mn = spec.rejection.min_trades_per_period
    for name, m in (("development", dev), ("validation", val)):
        if m.get("trades", 0) < mn:
            w.append(ResearchWarning("SMALL_SAMPLE", f"{name} has {m.get('trades', 0)} trades (< {mn}); statistics are not reliable"))
    d, v = dev.get("net_expectancy_r"), val.get("net_expectancy_r")
    if d is not None and v is not None and d > 0 and v < d * (1 - spec.rejection.max_validation_degradation):
        w.append(ResearchWarning("LARGE_OOS_DEGRADATION", f"validation expectancy {v:.3f}R vs development {d:.3f}R "
                                 f"(drop > {spec.rejection.max_validation_degradation:.0%})"))
    combined = pd.concat([dev_trades, val_trades]) if len(dev_trades) or len(val_trades) else dev_trades
    if len(combined):
        by_year = combined.groupby("year")["net_r"].sum()
        pos = by_year[by_year > 0].sum()
        if pos > 0 and by_year.max() / pos > 0.7 and len(by_year) > 1:
            w.append(ResearchWarning("SINGLE_YEAR_DEPENDENCE", f"{by_year.idxmax()} supplies {by_year.max() / pos:.0%} of positive net R"))
        if len(by_year) == 1:
            w.append(ResearchWarning("SINGLE_YEAR_DEPENDENCE", f"all trades fall in {by_year.index[0]}"))
    g = dev.get("gross_expectancy_r"), dev.get("net_expectancy_r")
    if g[0] is not None and g[1] is not None and g[0] > 0 >= g[1]:
        w.append(ResearchWarning("COST_FRAGILITY", "positive gross expectancy becomes non-positive after costs"))
    s2 = stress.get("spread_x2")
    if s2 is not None and v is not None and v > 0 >= s2.get("net_expectancy_r", 0):
        w.append(ResearchWarning("COST_FRAGILITY", "validation edge disappears with 2x spread"))
    if neighbours:
        base = v if v is not None else 0.0
        flips = sum(1 for nb in neighbours if (nb.get("net_expectancy_r") or 0) * base < 0)
        if flips / len(neighbours) > 0.3:
            w.append(ResearchWarning("UNSTABLE_NEIGHBOURING_PARAMETERS", f"{flips}/{len(neighbours)} parameter neighbours flip the sign of expectancy"))
    if n_family_trials >= 5 and val.get("t_stat") is not None:
        crit = multiple_testing(n_family_trials + 1)["critical_abs_t"]
        t = val["t_stat"]
        if t is not None and not (isinstance(t, float) and math.isnan(t)) and abs(t) < crit:
            w.append(ResearchWarning("MULTIPLE_TESTING_RISK", f"validation |t|={abs(t):.2f} below multiple-testing threshold {crit:.2f}"))
    return w
