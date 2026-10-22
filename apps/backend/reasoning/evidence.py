"""Build the bounded, deterministic evidence for an experiment question from stored Quant Agent results.
Reads only the registry (no market data is touched), so it can never reach the protected FINAL_UNTOUCHED window."""
from __future__ import annotations

from .review import EvidenceItem


def experiment_evidence(registry, agent, hid: str) -> tuple[EvidenceItem, dict]:
    """-> (EvidenceItem for the reviewer, compact `quant` dict for the decision engine). Includes the prespecified thresholds so a reviewer
    never has to say a failure 'cannot be confirmed' for lack of the limit."""
    pk = agent.evidence_packet(hid)
    row = registry.get(hid)
    spec = registry.spec(hid)
    dev, val = pk.get("development"), pk.get("validation")
    if not dev or not val:
        raise ValueError(f"{hid} has no stored development/validation results (status {pk['status']})")
    rc = spec.rejection
    s2 = (pk.get("sensitivity") or {}).get("spread_x2", {}).get("validation") or {}
    neigh = [n.get("net_expectancy_r") for n in (pk.get("parameter_neighbours") or []) if n.get("net_expectancy_r") is not None]
    quant = {"experiment_id": hid, "status": pk["status"], "net_expectancy_r": dev["net_expectancy_r"], "validation_net_expectancy_r": val["net_expectancy_r"],
             "gross_expectancy_r": dev["gross_expectancy_r"], "validation_gross_expectancy_r": val["gross_expectancy_r"], "dev_trades": dev["trades"],
             "val_trades": val["trades"], "failed_criteria": pk.get("failure_reason"), "spec_hash": pk["spec_hash"][:12], "t_stat_dev": dev.get("t_stat")}
    summary = (f"dev n={dev['trades']} net {dev['net_expectancy_r']:+.3f}R gross {dev['gross_expectancy_r']:+.3f}R (t={dev.get('t_stat', float('nan')):.2f}); "
               f"val n={val['trades']} net {val['net_expectancy_r']:+.3f}R gross {val['gross_expectancy_r']:+.3f}R (t={val.get('t_stat', float('nan')):.2f}); "
               f"val max drawdown {val.get('max_drawdown_r', float('nan')):.1f}R vs limit {rc.max_drawdown_r:g}R; min trades/period {rc.min_trades_per_period}; "
               f"required val net expectancy > {rc.min_net_expectancy_r:g}R; val net under 2x spread {s2.get('net_expectancy_r', float('nan')):+.3f}R; "
               f"parameter neighbours net: {', '.join(f'{x:+.3f}R' for x in neigh) or 'n/a'}; criteria failed: {pk.get('failure_reason')}")
    item = EvidenceItem(name=f"quant_agent_{hid}", kind="quant", as_of=row["updated_at"], freshness="CURRENT",
                        provenance=f"Quant Agent; data {pk.get('data_version')}; provider {spec.data.provider}; spec {pk['spec_hash'][:12]}; "
                                   f"periods dev {spec.development.start:%Y-%m-%d}..{spec.development.end:%Y-%m-%d}, val ..{spec.validation.end:%Y-%m-%d}",
                    summary=summary[:900], immutable=pk["status"] in ("REJECTED", "VALIDATED", "FAILED"))
    return item, quant
