"""Professional Intelligence Composer.

Formats and normalizes analytical output from the four engines (Market Research,
Chart Analysis, Strategy Evaluation, Trade Calculation) into clean, concise (~200-400 words),
institutional-grade Markdown.
"""
from __future__ import annotations

import re


_DEV_LEAK_PATTERNS = [
    re.compile(r"c:\\tars[^\s\n]*", re.IGNORECASE),
    re.compile(r"branch:\s*feature/[^\s\n]*", re.IGNORECASE),
    re.compile(r"commit\s*sha:[^\s\n]*", re.IGNORECASE),
    re.compile(r"my instructions restrict me to trading facts provided verbatim in a current state block", re.IGNORECASE),
    re.compile(r"i don't have that information\.\s*my instructions restrict me", re.IGNORECASE),
]


class IntelligenceComposer:
    """Sanitizes, formats, and composes intelligence responses."""

    @classmethod
    def sanitize_text(cls, text: str) -> str:
        """Strips out any unintentional internal developer or repository metadata leaks."""
        cleaned = text
        for pat in _DEV_LEAK_PATTERNS:
            cleaned = pat.sub("", cleaned)
        return cleaned.strip()

    @classmethod
    def format_chart_response(
        cls,
        *,
        instrument: str | None,
        timeframe: str | None,
        current_price_context: str | None,
        supply_zone: str | None,
        demand_zone: str | None,
        recent_price_sequence: str | None,
        bias: str | None,
        what_i_see: str | None,
        setup: str | None,
        key_levels: list[str] | None,
        invalidation: str | None,
        risk_notes: str | None,
        action: str | None,
        capital_note: str | None = None,
    ) -> str:
        """Formats chart analysis with explicit headings and bullets matching the spec."""
        sections: list[str] = []

        instrument_str = " · ".join(p for p in (instrument, timeframe) if p)
        if instrument_str:
            sections.append(f"### INSTRUMENT\n{instrument_str}")

        structure_bullets = []
        if current_price_context:
            structure_bullets.append(f"- Current price: {current_price_context}")
        if supply_zone:
            structure_bullets.append(f"- Supply zone: {supply_zone}")
        if demand_zone:
            structure_bullets.append(f"- Demand zone: {demand_zone}")
        if recent_price_sequence:
            structure_bullets.append(f"- Recent movement: {recent_price_sequence}")

        if structure_bullets:
            sections.append("### STRUCTURE\n" + "\n".join(structure_bullets))

        if bias:
            sections.append(f"BIAS: {bias}")

        if what_i_see:
            sections.append(f"### WHAT I SEE\n{what_i_see}")

        if setup:
            sections.append(f"### SETUP\n{setup}")

        if key_levels:
            levels_formatted = []
            for lvl in key_levels:
                lvl_str = lvl if lvl.startswith(("-", "•")) else f"- {lvl}"
                levels_formatted.append(lvl_str)
            sections.append("### KEY LEVELS\n" + "\n".join(levels_formatted))

        if invalidation:
            sections.append(f"### INVALIDATION\n{invalidation}")

        if risk_notes:
            sections.append(f"### RISK\n{risk_notes}")

        if action:
            sections.append(f"ACTION: {action}")

        if capital_note:
            sections.append(f"{capital_note}")

        return "\n\n".join(sections)
