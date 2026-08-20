"""Deterministic Trade Calculation Engine.

Calculates risk, position sizing, profit/loss scenarios, and risk-reward
ratios purely deterministically with zero model hallucination.

When trade parameters (entry, SL, TP, risk %, capital) are provided, it
calculates the exact mathematical outcome. When they are absent (e.g. "estimate
profit on 10000 rupees" without a trade plan), it explains clearly why profit
cannot be fabricated from a chart screenshot alone and lists the exact parameters
needed to calculate one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_CAPITAL_PROFIT_PATTERN = re.compile(
    r"\b(profit|loss|pnl|capital|rupees?|₹|\brs\.?\b|return on|roi|"
    r"how much (would|could|will) I (make|earn|lose|win)|"
    r"position size|money (would|could) I|what is the profit|estimate the profit)\b",
    re.IGNORECASE,
)

_NUM = r"[0-9]+(?:,[0-9]+)*(?:\.[0-9]+)?"

_ENTRY_PATTERN = re.compile(rf"\bentry(?:\s+(?:is|at|price))?\s*[:=]?\s*({_NUM})\b", re.IGNORECASE)
_SL_PATTERN = re.compile(rf"\b(?:stop\s*-?loss|sl)(?:\s+(?:is|at|price))?\s*[:=]?\s*({_NUM})\b", re.IGNORECASE)
_TP_PATTERN = re.compile(rf"\b(?:target|take\s*-?profit|tp)(?:\s+(?:is|at|price))?\s*[:=]?\s*({_NUM})\b", re.IGNORECASE)
_CAPITAL_AMOUNT_PATTERN = re.compile(
    rf"\b(?:capital|balance|account)(?:\s+(?:is|of|was))?\s*[:=]?\s*(?:₹|rs\.?|inr|\$)?\s*({_NUM})\b",
    re.IGNORECASE,
)
_RISK_PERCENT_PATTERN = re.compile(
    rf"\b(?:risk|risking)(?:\s+(?:is|of))?\s*[:=]?\s*({_NUM})\s*%",
    re.IGNORECASE,
)


@dataclass
class TradeCalculationParams:
    capital: float | None = None
    currency_symbol: str = "₹"
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    risk_percent: float | None = None
    leverage: float | None = None
    contract_size: float = 1.0


@dataclass
class TradeCalculationResult:
    has_sufficient_params: bool
    formatted_text: str
    risk_amount: float | None = None
    target_profit: float | None = None
    risk_reward_ratio: float | None = None
    position_size: float | None = None
    details: dict[str, Any] | None = None


def _parse_float(val: str | None) -> float | None:
    if not val:
        return None
    try:
        return float(val.replace(",", "").strip())
    except ValueError:
        return None


class TradeCalculationEngine:
    """Performs deterministic calculations for trading risk and profit/loss."""

    @classmethod
    def is_calculation_query(cls, text: str) -> bool:
        """Determines if the query is asking about capital, profit, loss, or position sizing."""
        return bool(_CAPITAL_PROFIT_PATTERN.search(text))

    @classmethod
    def extract_params(cls, text: str) -> TradeCalculationParams:
        """Extracts numerical trade parameters from text if present."""
        params = TradeCalculationParams()
        
        if "₹" in text or "rupee" in text.lower() or "rs" in text.lower() or "inr" in text.lower():
            params.currency_symbol = "₹"
        elif "$" in text or "usd" in text.lower() or "dollar" in text.lower():
            params.currency_symbol = "$"

        if cap_match := _CAPITAL_AMOUNT_PATTERN.search(text):
            params.capital = _parse_float(cap_match.group(1))

        if entry_match := _ENTRY_PATTERN.search(text):
            params.entry = _parse_float(entry_match.group(1))

        if sl_match := _SL_PATTERN.search(text):
            params.stop_loss = _parse_float(sl_match.group(1))

        if tp_match := _TP_PATTERN.search(text):
            params.take_profit = _parse_float(tp_match.group(1))

        if risk_match := _RISK_PERCENT_PATTERN.search(text):
            params.risk_percent = _parse_float(risk_match.group(1))

        return params

    @classmethod
    def evaluate(cls, query: str) -> TradeCalculationResult:
        """Calculates exact deterministic metrics or returns parameter requirements."""
        params = cls.extract_params(query)

        # Check if we have complete execution parameters for deterministic calculation
        if (
            params.capital is not None
            and params.entry is not None
            and params.stop_loss is not None
            and params.take_profit is not None
        ):
            risk_pct = params.risk_percent if params.risk_percent is not None else 1.0
            risk_amount = (params.capital * risk_pct) / 100.0
            risk_dist = abs(params.entry - params.stop_loss)
            reward_dist = abs(params.take_profit - params.entry)

            if risk_dist <= 0:
                return TradeCalculationResult(
                    has_sufficient_params=False,
                    formatted_text=(
                        "### CAPITAL / PROFIT ESTIMATE\n\n"
                        "Invalid price parameters: Stop Loss cannot equal Entry price."
                    ),
                )

            rr_ratio = reward_dist / risk_dist
            position_size = risk_amount / (risk_dist * params.contract_size)
            target_profit = risk_amount * rr_ratio

            curr = params.currency_symbol
            text = (
                f"### CAPITAL / PROFIT CALCULATION\n\n"
                f"• Capital: {curr}{params.capital:,.2f}\n"
                f"• Risk ({risk_pct:.1f}%): {curr}{risk_amount:,.2f}\n"
                f"• Entry: {params.entry}\n"
                f"• Stop Loss: {params.stop_loss} ({risk_dist:.2f} pts risk)\n"
                f"• Target: {params.take_profit} ({reward_dist:.2f} pts reward)\n"
                f"• Risk:Reward Ratio: 1:{rr_ratio:.2f}\n"
                f"• Calculated Position Size: {position_size:.4f} units\n"
                f"• Projected Profit at Target: {curr}{target_profit:,.2f}\n\n"
                f"*Note: This is a deterministic mathematical projection based on your inputs, "
                f"not a guaranteed market outcome.*"
            )

            return TradeCalculationResult(
                has_sufficient_params=True,
                formatted_text=text,
                risk_amount=risk_amount,
                target_profit=target_profit,
                risk_reward_ratio=rr_ratio,
                position_size=position_size,
                details={
                    "capital": params.capital,
                    "risk_amount": risk_amount,
                    "target_profit": target_profit,
                    "rr_ratio": rr_ratio,
                },
            )

        # Incomplete parameters: explain why profit cannot be guessed
        explanation = (
            "### CAPITAL / PROFIT ESTIMATE\n\n"
            "A responsible profit estimate requires explicit trade parameters:\n\n"
            "- entry\n"
            "- stop loss\n"
            "- target\n"
            "- risk percentage or position size\n"
            "- leverage/contract specification where relevant\n\n"
            "I'm not going to invent a rupee figure from the chart image alone. "
            "Visual chart observation alone does not define an execution plan. "
            "Provide your planned entry, stop loss, target, and risk, and I will compute the exact "
            "mathematical position sizing and risk:reward profile."
        )

        return TradeCalculationResult(
            has_sufficient_params=False,
            formatted_text=explanation,
        )
