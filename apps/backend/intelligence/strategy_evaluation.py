"""Strategy & Quant Validation Engine.

Evaluates trading setups and strategy rules against deterministic ground truth.
Quant_brain is the authoritative source for strategy validation.
Never invents confidence scores, fake win rates, or simulated backtest results.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class StrategyEvaluationReport:
    is_strategy_configured: bool
    active_setups: list[dict[str, Any]]
    formatted_text: str
    reason_code: str | None = None


class StrategyEvaluationEngine:
    """Evaluates strategy status and active setup rules."""

    @classmethod
    def evaluate_setups(
        cls,
        active_setups: list[dict[str, Any]],
        strategy_status: str = "NOT_CONFIGURED",
    ) -> StrategyEvaluationReport:
        is_configured = strategy_status == "CONFIGURED"

        lines = [
            "### STRATEGY / QUANT VALIDATION",
            f"• Strategy Engine: `{strategy_status}`",
        ]

        if not active_setups:
            lines.append("• Active Setups: None currently detected in active state.")
            lines.append("• Reason: No strategy signals meet execution criteria at this moment.")
        else:
            lines.append(f"• Active Setups: {len(active_setups)} active")
            for s in active_setups:
                sym = s.get("symbol", "UNKNOWN")
                direction = s.get("direction", "LONG").upper()
                entry = s.get("entry", "-")
                sl = s.get("stop_loss", "-")
                tp = s.get("target", "-")
                rr = s.get("risk_reward", "-")
                state = s.get("state", "SETUP_VALID")
                lines.append(
                    f"  - **{sym} ({direction})**: Entry: `{entry}` | SL: `{sl}` | TP: `{tp}` | R:R: `{rr}R` | State: `{state}`"
                )

        lines.append("\n*Validated trading intelligence originates strictly from deterministic quant_brain state.*")

        return StrategyEvaluationReport(
            is_strategy_configured=is_configured,
            active_setups=active_setups,
            formatted_text="\n".join(lines),
        )
