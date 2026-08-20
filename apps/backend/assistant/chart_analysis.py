"""Chart-analysis service: TARS's "Analyze this chart" flow.

Reuses the existing AssistantProvider interface (always a ClaudeCodeProvider
here -- see assistant/factory.py's build_chart_assistant_provider()) rather
than introducing a second AI/provider framework. Takes a screenshot that was
already produced by the real, backend-authorized capture flow (ActionRuntime
-> windows_app skill -> FrontendCommandBridge, see actions/frontend_bridge.py)
and the active-window context alongside it, asks Claude for a qualitative,
uncertainty-aware read, and returns a structured (but honestly-degraded-on-
parse-failure) result.

quant_brain remains the only source of validated strategy claims. This
service never emits a confidence percentage or a guaranteed prediction, and
every result carries a disclaimer that it is a qualitative companion read,
not a quant_brain-equivalent signal.
"""
from __future__ import annotations

import io
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from assistant.provider import AssistantProvider, AssistantRequest
from intelligence.composer import IntelligenceComposer
from intelligence.trade_calculation import TradeCalculationEngine

_SYSTEM_PROMPT = (
    "You are TARS, an institutional AI quantitative trading companion analyzing a screenshot "
    "of the user's active chart. "
    "Provide decision-oriented, institutional market structure analysis. "
    "Do NOT give elementary visual narration (e.g. 'price moved up', 'green candle visible'). "
    "Identify key market regime, order flow imbalances, supply/demand zones, and swing structure. "
    "You are a companion, not quant_brain -- never state a confidence percentage, "
    "never claim a prediction is guaranteed, never claim an unvalidated setup is a statistical edge, "
    "and never invent price levels not clearly legible in the image. "
    "If the image is not a chart, state that plainly. "
    "Keep every value concise, high-signal, and professional. "
    "Keep the ENTIRE response under 350 words total.\n\n"
    "You must ALWAYS answer in the exact JSON format below, no matter what else the user asks. "
    "Never switch to plain prose outside the JSON object. "
    "Never calculate or state a profit or loss amount in any currency from the chart image alone.\n\n"
    "Respond with ONLY a single JSON object with exactly these keys:\n"
    '- "instrument": string or null (ticker/symbol if legible, e.g. "XAUUSD")\n'
    '- "timeframe": string or null (e.g. "15M", "4H", "1D")\n'
    '- "current_price_context": string or null (current price and immediate context, e.g. "2684.50, testing supply zone")\n'
    '- "supply_zone": string or null (nearest visible supply/resistance level or zone)\n'
    '- "demand_zone": string or null (nearest visible demand/support level or zone)\n'
    '- "recent_price_sequence": string (structural trend/range regime and order flow dynamics)\n'
    '- "at_meaningful_location": boolean (true only if price is visibly at or near a marked zone/level)\n'
    '- "bias": string ("Bullish" | "Bearish" | "Neutral")\n'
    '- "what_i_see": string (2-3 concise structural observations)\n'
    '- "setup": string ("Valid" | "Invalid" | "Unclear")\n'
    '- "key_levels": array of short strings with level labels (e.g. ["Resistance: 2700.00", "Support: 2660.00", "Demand: 2645.00"])\n'
    '- "possible_setup": string or null (e.g. "Liquidity Sweep & Supply Retest")\n'
    '- "invalidation": string or null (specific condition/level invalidating this read)\n'
    '- "risk_notes": string (concise volatility, session timing, or liquidity risk note)\n'
    '- "action": string ("Wait" | "Watch" | "Potential setup")\n'
    '- "market_context": string (concise overview sentence)'
)

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


_TRADE_PARAMS_PATTERN = re.compile(
    r"\b(entry|stop\s*-?loss|\bsl\b|target|take\s*-?profit|\btp\b|risk\s*%|risk\s*percent|lot\s*size|leverage)\b",
    re.IGNORECASE,
)


def _split_capital_question(goal_text: str) -> tuple[str, str | None]:
    """Separates the visual chart request from capital/profit estimation.
    Profit calculations are handled deterministically by TradeCalculationEngine."""
    if TradeCalculationEngine.is_calculation_query(goal_text) and not _TRADE_PARAMS_PATTERN.search(goal_text):
        calc_result = TradeCalculationEngine.evaluate(goal_text)
        return "Analyze this chart.", calc_result.formatted_text
    return goal_text, None


_STRUCTURED_KEYS = (
    "instrument",
    "timeframe",
    "market_context",
    "key_levels",
    "possible_setup",
    "invalidation",
    "risk_notes",
)


@dataclass
class ChartAnalysisResult:
    instrument: str | None
    timeframe: str | None
    market_context: str
    key_levels: list[str]
    possible_setup: str | None
    invalidation: str | None
    risk_notes: str
    provider: str
    raw_text: str
    # False if the model's reply couldn't be parsed as the requested JSON
    # shape -- market_context then holds the raw text verbatim instead of
    # fabricated structure.
    structured: bool
    bias: str | None = None
    what_i_see: str | None = None
    setup: str | None = None
    action: str | None = None
    # Observed-only grounding fields (part of the sharpened prompt to match
    # direct-Claude's structure) -- distinct from `setup`/`bias`, which are
    # this companion's qualitative read, not a validated signal.
    current_price_context: str | None = None
    supply_zone: str | None = None
    demand_zone: str | None = None
    recent_price_sequence: str | None = None
    at_meaningful_location: bool | None = None
    # Set deterministically (never model-generated) when the user asked
    # about profit/capital without supplying entry/stop/target/risk --
    # see _wants_capital_estimate_without_params(). None otherwise.
    capital_note: str | None = None
    disclaimer: str = (
        "Qualitative read from TARS's assistant, not a quant_brain-validated "
        "signal. No confidence score; nothing here is a guaranteed outcome."
    )

    def to_dict(self) -> dict[str, Any]:
        # Includes speech_text and formatted_tars_text
        return {
            **asdict(self),
            "speech_text": self.speech_text(),
            "formatted_tars_text": self.formatted_tars_text(),
        }

    def formatted_tars_text(self) -> str:
        """Returns the full structured TARS response as real Markdown
        (headings + bullets) -- the complete analysis, never a flattened
        single paragraph and never truncated."""
        if not self.structured:
            parts = [IntelligenceComposer.sanitize_text(self.market_context.strip())] if self.market_context.strip() else []
            if self.capital_note:
                parts.append(self.capital_note)
            return "\n\n".join(parts) if parts else "I couldn't produce a read from that image."

        return IntelligenceComposer.format_chart_response(
            instrument=self.instrument,
            timeframe=self.timeframe,
            current_price_context=self.current_price_context,
            supply_zone=self.supply_zone,
            demand_zone=self.demand_zone,
            recent_price_sequence=self.recent_price_sequence,
            bias=self.bias or "Neutral",
            what_i_see=self.what_i_see or self.market_context,
            setup=self.setup or "Unclear",
            key_levels=self.key_levels,
            invalidation=self.invalidation or "Structure break",
            risk_notes=self.risk_notes or "Standard market risk.",
            action=self.action or "Watch",
            capital_note=self.capital_note,
        )

    def speech_text(self) -> str:
        """A short, spoken-friendly summary for TTS."""
        if not self.structured:
            # The model didn't return the requested JSON shape -- there is
            # no parsed bias/setup to report, and claiming "Bias is
            # Neutral" here would be fabricating a read that was never
            # actually produced. Speak a markdown-stripped, capped version
            # of its real raw text instead.
            return _strip_markdown_for_speech(self.market_context)

        parts: list[str] = []
        bias_label = self.bias or "Neutral"
        label = " ".join(p for p in (self.instrument, self.timeframe) if p)
        if label:
            parts.append(f"Bias is {bias_label} on {label}.")
        else:
            parts.append(f"Bias is {bias_label}.")
        if self.current_price_context:
            parts.append(self.current_price_context + ".")
        if self.what_i_see:
            parts.append(self.what_i_see)
        elif self.market_context:
            parts.append(self.market_context)
        if self.invalidation:
            parts.append(f"Invalidation condition: {self.invalidation}.")
        if self.action:
            parts.append(f"Action: {self.action}.")
        elif self.possible_setup:
            parts.append(f"Possible read: {self.possible_setup}.")
        if self.capital_note:
            parts.append(
                "For a profit estimate I'd need your entry, stop loss, target, "
                "and position size -- I won't guess a number from the chart alone."
            )
        return " ".join(p.strip() for p in parts if p and p.strip())


class ChartAnalysisError(RuntimeError):
    """Raised when the supplied image bytes can't actually be decoded --
    never silently sent on to the model as if it were viewable."""


# Written inside the project tree (apps/backend/storage/tmp/), not the OS
# temp directory: ClaudeCodeProvider drives a `claude` CLI subprocess whose
# Read tool is scoped to its own working directory by default, and
# depending on `--add-dir` to reach an OS temp path adds a failure mode
# across environments/sandboxes for no benefit -- a location this backend
# already controls end to end is simpler and more portable.
_SCRATCH_DIR = Path(__file__).resolve().parent.parent / "storage" / "tmp"


class ChartAnalysisService:
    def __init__(self, provider: AssistantProvider):
        self._provider = provider

    async def analyze(
        self,
        *,
        image_bytes: bytes,
        image_format: str,
        conversation_id: str,
        active_context_text: str = "",
        goal_text: str = "Analyze this chart.",
    ) -> ChartAnalysisResult:
        # Win32 GDI captures come back as BMP; Claude Code's Read tool
        # reliably supports PNG but not BMP, so re-encode rather than risk
        # a silent "can't view this" from the model. A real decode failure
        # (corrupt/truncated capture) raises rather than shipping garbage.
        try:
            image = Image.open(io.BytesIO(image_bytes))
            image.load()
        except UnidentifiedImageError as exc:
            raise ChartAnalysisError(
                f"Captured image bytes could not be decoded (declared format "
                f"'{image_format}'); refusing to send an unreadable capture "
                "to the model."
            ) from exc

        vision_goal, capital_note = _split_capital_question(goal_text)

        _SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        image_path = _SCRATCH_DIR / f"chart-{uuid4().hex}.png"
        image.convert("RGB").save(image_path, format="PNG")
        try:
            system_context = _SYSTEM_PROMPT
            if active_context_text:
                system_context += (
                    "\n\nActive window context (for grounding only, not "
                    f"necessarily part of the chart itself): {active_context_text}"
                )

            request = AssistantRequest(
                text=vision_goal,
                conversation_id=conversation_id,
                system_context=system_context,
                image_path=str(image_path),
            )
            reply = await self._provider.respond(request)
        finally:
            image_path.unlink(missing_ok=True)

        result = _parse_reply(reply.text, reply.provider)
        result.capital_note = capital_note
        return result

    async def analyze_stream(
        self,
        *,
        image_bytes: bytes,
        image_format: str,
        conversation_id: str,
        active_context_text: str = "",
        goal_text: str = "Analyze this chart.",
    ):
        # Server-side stage timings, in ms from this call's own start --
        # capture_ms (screenshot -> bytes reaching this backend) is measured
        # client-side, since capture happens entirely outside this process;
        # everything from here on is measured server-side so the frontend
        # can show real, non-fabricated numbers rather than guessing.
        t0 = time.monotonic()
        yield {"type": "status", "text": "Looking at the chart..."}

        try:
            image = Image.open(io.BytesIO(image_bytes))
            image.load()
        except UnidentifiedImageError as exc:
            raise ChartAnalysisError(
                f"Captured image bytes could not be decoded (declared format "
                f"'{image_format}'); refusing to send an unreadable capture "
                "to the model."
            ) from exc

        vision_goal, capital_note = _split_capital_question(goal_text)

        _SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        image_path = _SCRATCH_DIR / f"chart-{uuid4().hex}.png"
        image.convert("RGB").save(image_path, format="PNG")
        try:
            system_context = _SYSTEM_PROMPT
            if active_context_text:
                system_context += (
                    "\n\nActive window context (for grounding only, not "
                    f"necessarily part of the chart itself): {active_context_text}"
                )

            request = AssistantRequest(
                text=vision_goal,
                conversation_id=conversation_id,
                system_context=system_context,
                image_path=str(image_path),
            )
            claude_start_ms = round((time.monotonic() - t0) * 1000)
            first_token_ms: int | None = None
            if hasattr(self._provider, "respond_stream"):
                async for event in self._provider.respond_stream(request):
                    if event.get("type") == "status":
                        # Real provider-observed milestone (e.g. the model
                        # has actually received the image), not a fabricated
                        # progress tick -- forward it as-is.
                        yield {"type": "status", "text": event.get("text", "")}
                    elif event.get("type") == "delta":
                        if first_token_ms is None:
                            first_token_ms = round((time.monotonic() - t0) * 1000)
                        yield {"type": "delta", "text": event.get("text", "")}
                    elif event.get("type") == "complete":
                        final_text = event.get("text", "")
                        provider_name = event.get("provider", self._provider.name)
                        result = _parse_reply(final_text, provider_name)
                        result.capital_note = capital_note
                        yield {
                            "type": "complete",
                            "result": result.to_dict(),
                            "timing": {
                                "claude_start_ms": claude_start_ms,
                                "first_token_ms": first_token_ms,
                                "complete_ms": round((time.monotonic() - t0) * 1000),
                            },
                        }
            else:
                reply = await self._provider.respond(request)
                result = _parse_reply(reply.text, reply.provider)
                result.capital_note = capital_note
                yield {
                    "type": "complete",
                    "result": result.to_dict(),
                    "timing": {
                        "claude_start_ms": claude_start_ms,
                        "first_token_ms": None,
                        "complete_ms": round((time.monotonic() - t0) * 1000),
                    },
                }
        finally:
            image_path.unlink(missing_ok=True)


def _parse_reply(text: str, provider: str) -> ChartAnalysisResult:
    match = _JSON_OBJECT.search(text)
    payload: Any = None
    if match:
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            payload = None

    if isinstance(payload, dict) and all(key in payload for key in _STRUCTURED_KEYS):
        key_levels = payload.get("key_levels")
        if not isinstance(key_levels, list):
            key_levels = []
        return ChartAnalysisResult(
            instrument=_opt_str(payload.get("instrument")),
            timeframe=_opt_str(payload.get("timeframe")),
            market_context=str(payload.get("market_context") or ""),
            key_levels=[str(level) for level in key_levels],
            possible_setup=_opt_str(payload.get("possible_setup")),
            invalidation=_opt_str(payload.get("invalidation")),
            risk_notes=str(payload.get("risk_notes") or ""),
            bias=_opt_str(payload.get("bias")),
            what_i_see=_opt_str(payload.get("what_i_see")),
            setup=_opt_str(payload.get("setup")),
            action=_opt_str(payload.get("action")),
            current_price_context=_opt_str(payload.get("current_price_context")),
            supply_zone=_opt_str(payload.get("supply_zone")),
            demand_zone=_opt_str(payload.get("demand_zone")),
            recent_price_sequence=_opt_str(payload.get("recent_price_sequence")),
            at_meaningful_location=(
                payload.get("at_meaningful_location")
                if isinstance(payload.get("at_meaningful_location"), bool)
                else None
            ),
            provider=provider,
            raw_text=text,
            structured=True,
        )

    # The model didn't return the requested shape -- surface its real text
    # rather than fabricating structure that isn't there.
    return ChartAnalysisResult(
        instrument=None,
        timeframe=None,
        market_context=text.strip(),
        key_levels=[],
        possible_setup=None,
        invalidation=None,
        risk_notes="Response was not returned in the requested structured format.",
        provider=provider,
        raw_text=text,
        structured=False,
    )


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


_MARKDOWN_EMPHASIS = re.compile(r"\*\*([^*]+)\*\*|\*([^*]+)\*|__([^_]+)__|_([^_]+)_")
_MARKDOWN_HEADER_OR_BULLET = re.compile(r"^\s{0,3}(#{1,6}|[-*]|\d+\.)\s+", re.MULTILINE)
_WHITESPACE_RUN = re.compile(r"\s+")
_SPEECH_CHAR_LIMIT = 600


def _strip_markdown_for_speech(text: str) -> str:
    """Voice output can't speak `**bold**`/`### headers`/`- bullets`
    literally -- used only for the unstructured fallback path, where the
    model's raw reply is markdown-formatted prose rather than the
    requested JSON."""
    if not text:
        return "I looked at the chart but couldn't produce a structured read this time."
    cleaned = _MARKDOWN_HEADER_OR_BULLET.sub("", text)
    cleaned = _MARKDOWN_EMPHASIS.sub(lambda m: next(g for g in m.groups() if g is not None), cleaned)
    cleaned = _WHITESPACE_RUN.sub(" ", cleaned).strip()
    if len(cleaned) > _SPEECH_CHAR_LIMIT:
        cleaned = cleaned[:_SPEECH_CHAR_LIMIT].rsplit(" ", 1)[0] + "..."
    return cleaned
