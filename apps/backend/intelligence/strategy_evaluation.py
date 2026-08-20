"""Strategy & Quant Validation Engine.

Evaluates trading setups and strategy rules against deterministic ground truth.
Quant_brain is the authoritative source for strategy validation.
Never invents confidence scores, fake win rates, or simulated backtest results.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_ENTRY_INQUIRY_PATTERN = re.compile(
    r"\b(should\s+i\s+enter|can\s+i\s+enter|is\s+it\s+time\s+to\s+enter|enter\s+now|take\s+the\s+trade|buy\s+now|sell\s+now)\b",
    re.IGNORECASE,
)


@dataclass
class StrategyEvaluationReport:
    is_strategy_configured: bool
    active_setups: list[dict[str, Any]]
    formatted_text: str
    reason_code: str | None = None


class StrategyEvaluationEngine:
    """Evaluates strategy status and active setup rules."""

    @classmethod
    def is_entry_inquiry(cls, text: str) -> bool:
        """Checks if the user is asking whether to enter a trade."""
        return bool(_ENTRY_INQUIRY_PATTERN.search(text))

    @classmethod
    def evaluate_entry_decision(
        cls,
        active_setups: list[dict[str, Any]],
        symbol: str | None = None,
    ) -> str:
        """Returns deterministic entry evaluation. Without quant_brain validated trigger: NO VALIDATED TRADE."""
        valid_setups = [
            s for s in active_setups
            if s.get("validation_status") == "VALID"
            and (symbol is None or s.get("symbol", "").upper() == symbol.upper())
        ]

        if not valid_setups:
            return (
                "### STRATEGY EVALUATION\n\n"
                "NO VALIDATED TRADE.\n\n"
                "• Status: No active setup has met quant_brain statistical validation criteria.\n"
                "• Action: Stand aside. TARS is a companion and never enters or recommends execution "
                "without an authoritative, validated trigger from quant_brain."
            )

        setup = valid_setups[0]
        sym = setup.get("symbol", "UNKNOWN")
        direction = (setup.get("direction") or "LONG").upper()
        entry = setup.get("entry", "-")
        sl = setup.get("stop_loss", "-")
        tp = setup.get("target", "-")
        rr = setup.get("risk_reward", "-")

        return (
            f"### STRATEGY EVALUATION\n\n"
            f"VALIDATED SETUP DETECTED: **{sym} ({direction})**\n\n"
            f"• Entry: `{entry}`\n"
            f"• Stop Loss: `{sl}`\n"
            f"• Target: `{tp}`\n"
            f"• Risk:Reward: `{rr}R`\n"
            f"• State: `{setup.get('state', 'SETUP_VALID')}`\n\n"
            f"*Validated by quant_brain engine.*"
        )

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
            lines.append("• Status: NO VALIDATED TRADE.")
            lines.append("• Reason: No strategy signals meet execution criteria at this moment.")
        else:
            lines.append(f"• Active Setups: {len(active_setups)} active")
            for s in active_setups:
                sym = s.get("symbol", "UNKNOWN")
                direction = (s.get("direction") or "LONG").upper()
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
