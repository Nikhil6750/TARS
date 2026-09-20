"""Trading Intelligence foundation — domain models.

TARS is a companion, not `quant_brain`: this module defines the *shape* of
strategy-aware reasoning (a `StrategyDefinition`, the `TradingContext` a
skill/agent hands to the assistant, and a `TradingAnalysis` result) without
inventing any actual strategy rules. See `trading/provider.py` for why the
default `StrategyProvider` always reports `NOT_CONFIGURED` — that is not a
placeholder to be "filled in" casually; it is the correct, honest answer
until a real strategy source (quant_brain or an explicit local config) is
wired in. See ARCHITECTURE.md § quant_brain boundary and MASTER_SPEC.md's
"not a source of fabricated confidence" principle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class StrategyStatus(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    CONFIGURED = "CONFIGURED"


@dataclass(frozen=True)
class StrategyDefinition:
    """A strategy's identity and *validated* parameters, as reported by
    whatever configured `StrategyProvider` sourced it (ultimately
    quant_brain, or an explicit local config — never invented here).
    `rules_summary` is a human-readable description for grounding text, not
    a machine-executable rule set TARS evaluates itself; TARS does not
    reimplement strategy logic (ARCHITECTURE.md § quant_brain boundary)."""

    strategy_id: str
    name: str
    rules_summary: str
    source: str  # e.g. "quant_brain" | "local_config"
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TradingContext:
    """The deterministic trading state a skill/agent hands to the assistant
    as grounding — mirrors what `assistant/grounding.py` already builds from
    `active_setups`, but packaged for trading-specific skills/agents so they
    don't reach into `EventService` and `StrategyProvider` independently."""

    strategy_status: StrategyStatus
    active_setups: list[dict[str, Any]] = field(default_factory=list)
    recent_warnings: list[dict[str, Any]] = field(default_factory=list)
    strategy: StrategyDefinition | None = None
    generated_at: datetime | None = None

    def as_grounding_text(self) -> str:
        lines: list[str] = []
        if self.strategy_status == StrategyStatus.NOT_CONFIGURED:
            lines.append(
                "Strategy status: NOT_CONFIGURED — no strategy source is wired in. "
                "Do not invent strategy rules or claim a setup matches a strategy."
            )
        else:
            lines.append(f"Strategy status: CONFIGURED ({self.strategy.name if self.strategy else 'unknown'}).")
        if self.active_setups:
            lines.append(f"{len(self.active_setups)} active setup(s) currently tracked.")
        else:
            lines.append("No active setups currently tracked.")
        if self.recent_warnings:
            lines.append(f"{len(self.recent_warnings)} recent warning(s).")
        return "\n".join(lines)


@dataclass(frozen=True)
class TradingAnalysis:
    """Result of a qualitative trading analysis (chart read, setup
    explanation, ...). Deliberately has no confidence score and no
    guaranteed-outcome field — see `ChartAnalysisResult` in
    `assistant/chart_analysis.py`, which this mirrors for non-chart
    analyses (e.g. `explain_setup`)."""

    summary: str
    strategy_status: StrategyStatus
    symbol: str | None = None
    key_points: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    disclaimer: str = (
        "Qualitative companion analysis, not a quant_brain-validated signal. "
        "No confidence score; nothing here is a guaranteed outcome."
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "strategy_status": self.strategy_status.value,
            "symbol": self.symbol,
            "key_points": self.key_points,
            "caveats": self.caveats,
            "disclaimer": self.disclaimer,
        }
