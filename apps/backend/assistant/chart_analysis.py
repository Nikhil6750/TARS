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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from assistant.provider import AssistantProvider, AssistantRequest

_SYSTEM_PROMPT = (
    "You are TARS, a trading companion, looking at a screenshot of the "
    "user's active window (likely a charting application) on their "
    "explicit request to analyze it. You are not quant_brain and this is "
    "not a validated trading signal -- never state a confidence percentage, "
    "never claim a prediction is guaranteed, and never invent a price level "
    "you cannot actually read from the image. If the image is not a chart, "
    "say so plainly instead of inventing chart content. Respond with ONLY a "
    "single JSON object (no markdown fences, no prose outside it) with "
    'exactly these keys: "instrument" (string or null -- ticker/symbol if '
    'legible, else null), "timeframe" (string or null), "market_context" '
    '(1-3 sentences of what the price action/structure actually shows), '
    '"key_levels" (array of short strings naming visible support/resistance '
    "or structural levels -- empty array if none are legible), "
    '"possible_setup" (string or null -- a qualitative, hedged read of what '
    "setup this *could* be, never phrased as a recommendation or "
    'certainty), "invalidation" (string or null -- what would invalidate '
    'that read), "risk_notes" (string -- uncertainty, missing context, or '
    "reasons this read could be wrong)."
)

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

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
    disclaimer: str = (
        "Qualitative read from TARS's assistant, not a quant_brain-validated "
        "signal. No confidence score; nothing here is a guaranteed outcome."
    )

    def to_dict(self) -> dict[str, Any]:
        # Includes speech_text so the frontend speaks/display the exact same
        # composed summary this backend computed, rather than re-deriving
        # its own formatting logic from the structured fields.
        return {**asdict(self), "speech_text": self.speech_text()}

    def speech_text(self) -> str:
        """A short, spoken-friendly summary for TTS."""
        parts: list[str] = []
        if self.instrument or self.timeframe:
            label = " ".join(p for p in (self.instrument, self.timeframe) if p)
            parts.append(f"Looking at {label}.")
        if self.market_context:
            parts.append(self.market_context)
        if self.possible_setup:
            parts.append(f"Possible read: {self.possible_setup}.")
        if self.invalidation:
            parts.append(f"That would be invalidated if {self.invalidation}.")
        parts.append("This isn't a validated signal, just a qualitative read.")
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
                text=goal_text,
                conversation_id=conversation_id,
                system_context=system_context,
                image_path=str(image_path),
            )
            reply = await self._provider.respond(request)
        finally:
            image_path.unlink(missing_ok=True)

        return _parse_reply(reply.text, reply.provider)


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
