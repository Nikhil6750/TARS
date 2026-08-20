"""Professional Intelligence Composer.

Formats and normalizes analytical output from the intelligence engines (Market Research,
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

_UNSUPPORTED_CHART_TERMS = [
    (re.compile(r"\bmajor\s+liquidity\s+pools?\b", re.IGNORECASE), "visible support/demand boundary"),
    (re.compile(r"\bliquidity\s+pools?\b", re.IGNORECASE), "price range boundary"),
    (re.compile(r"\border\s*flow\s+shows\b", re.IGNORECASE), "visible price action indicates"),
    (re.compile(r"\border\s*flow\s+imbalances?\b", re.IGNORECASE), "directional price movement"),
    (re.compile(r"\bconfirmed\s+order\s*flow\s+and\s+liquidity\s+absorption\b", re.IGNORECASE), "buyers holding above support"),
    (re.compile(r"\binstitutional\s+accumulation\b", re.IGNORECASE), "consolidation"),
    (re.compile(r"\binstitutional\s+activity\b", re.IGNORECASE), "visible market structure"),
    (re.compile(r"\bupcoming\s+(?:high-impact\s+)?macro\s+announcements?\b", re.IGNORECASE), "unobserved macro context"),
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
    def sanitize_chart_text(cls, text: str) -> str:
        """Normalizes unsupported speculative terms into evidence-grounded chart descriptions."""
        cleaned = cls.sanitize_text(text)
        for pat, repl in _UNSUPPORTED_CHART_TERMS:
            cleaned = pat.sub(repl, cleaned)
        return cleaned

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
        """Formats chart analysis using strictly evidence-grounded visible data."""
        sections: list[str] = []

        instrument_str = " · ".join(p for p in (instrument, timeframe) if p)
        if instrument_str:
            sections.append(f"### INSTRUMENT\n{instrument_str}")

        # 1. Structure
        structure_bullets = []
        if current_price_context:
            structure_bullets.append(f"- Current price: {cls.sanitize_chart_text(current_price_context)}")
        if supply_zone:
            structure_bullets.append(f"- Supply zone: {cls.sanitize_chart_text(supply_zone)}")
        if demand_zone:
            structure_bullets.append(f"- Demand zone: {cls.sanitize_chart_text(demand_zone)}")
        if recent_price_sequence:
            structure_bullets.append(f"- Recent movement: {cls.sanitize_chart_text(recent_price_sequence)}")

        if structure_bullets:
            sections.append("### STRUCTURE\n" + "\n".join(structure_bullets))

        # 2. Bias
        bias_str = bias or "Neutral"
        sections.append(f"BIAS: {bias_str}")

        # 3. Market State & Scenarios
        is_bull = "bull" in bias_str.lower()
        is_bear = "bear" in bias_str.lower()
        if is_bull:
            bull_desc = "Primary continuation toward upper resistance/supply zone if price holds above marked support."
            bear_desc = f"Structure breakdown if price loses demand zone ({demand_zone or 'support'}) on visible candles."
        elif is_bear:
            bull_desc = f"Structure reversal if price reclaims supply zone ({supply_zone or 'resistance'}) on visible candles."
            bear_desc = "Primary continuation toward lower support/demand zone as price remains below resistance."
        else:
            bull_desc = f"Upside breakout above range high / resistance ({supply_zone or 'resistance'})."
            bear_desc = f"Downside breakdown below range low / support ({demand_zone or 'support'})."

        clean_location = cls.sanitize_chart_text(current_price_context) if current_price_context else "Testing active range"
        sections.append(f"### MARKET STATE\n- Bias: **{bias_str}**\n- Location: {clean_location}")
        sections.append(f"### BULLISH SCENARIO\n{bull_desc}")
        sections.append(f"### BEARISH SCENARIO\n{bear_desc}")

        # 4. What I See
        if what_i_see:
            sections.append(f"### WHAT I SEE\n{cls.sanitize_chart_text(what_i_see)}")

        # 5. Setup & Trade Status
        setup_str = setup or "Unclear"
        sections.append(f"### SETUP\n{setup_str}")
        sections.append(
            f"### TRADE STATUS\n**NO VALIDATED TRADE** ({setup_str} setup read)\n"
            f"• Note: Visual observation only. Execution requires an active quant_brain statistical trigger."
        )

        # 6. Key Levels
        if key_levels:
            levels_formatted = []
            for lvl in key_levels:
                clean_lvl = cls.sanitize_chart_text(lvl)
                lvl_str = clean_lvl if clean_lvl.startswith(("-", "•")) else f"- {clean_lvl}"
                levels_formatted.append(lvl_str)
            sections.append("### KEY LEVELS\n" + "\n".join(levels_formatted))

        # 7. Invalidation
        if invalidation:
            sections.append(f"### INVALIDATION\n{cls.sanitize_chart_text(invalidation)}")

        # 8. Risk (Strictly Chart-Only Warning)
        mandatory_macro_risk_warning = "Macro/event risk has not been checked in this chart-only analysis."
        if risk_notes and risk_notes.strip():
            clean_risk = cls.sanitize_chart_text(risk_notes.strip())
            if mandatory_macro_risk_warning.lower() not in clean_risk.lower():
                risk_section_text = f"{clean_risk}\n• {mandatory_macro_risk_warning}"
            else:
                risk_section_text = clean_risk
        else:
            risk_section_text = mandatory_macro_risk_warning

        sections.append(f"### RISK\n{risk_section_text}")

        # 9. Action
        action_str = action or "Watch"
        sections.append(f"ACTION: {action_str}")

        if capital_note:
            sections.append(f"{capital_note}")

        return "\n\n".join(sections)
