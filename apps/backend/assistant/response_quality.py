"""Deterministic response presentation and quality checks.

The provider produces one evidence-grounded answer.  This module turns it
into two explicit representations:

* ``display_text`` retains useful Markdown for the chat surface.
* ``speech_text`` is plain, conversational text suitable for TTS.

The checks are intentionally lightweight.  They catch common regressions and
perform safe mechanical cleanup, but never make a second model call or pretend
to prove semantic correctness.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class ResponseComplexity(str, Enum):
    SHORT = "short"
    ORDINARY = "ordinary"
    COMPLEX = "complex"


@dataclass(frozen=True)
class ResponseQualityAssessment:
    directness: bool
    completeness: bool
    grounding: bool
    uncertainty: bool
    structure: bool
    user_mode_cleanliness: bool
    speech_suitability: bool
    issues: tuple[str, ...] = ()

    @property
    def passed(self) -> int:
        return sum(
            (
                self.directness,
                self.completeness,
                self.grounding,
                self.uncertainty,
                self.structure,
                self.user_mode_cleanliness,
                self.speech_suitability,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResponsePresentation:
    display_text: str
    speech_text: str
    quality: ResponseQualityAssessment

    def to_dict(self) -> dict[str, Any]:
        return {
            "display_text": self.display_text,
            "speech_text": self.speech_text,
            "quality": self.quality.to_dict(),
        }


QUALITY_SYSTEM_PROMPT = """
Answer the user's actual request immediately. Avoid generic preambles and filler.
Match the requested depth: short questions get short answers; multi-part or complex
requests get complete, well-structured answers. Preserve relevant conversation
context. If evidence is absent or incomplete, say exactly what is unknown and do
not invent facts. Trading claims must come only from the deterministic context;
without a validated trigger, say NO VALIDATED TRADE. Keep subprocess details,
provider internals, local paths, and implementation diagnostics out of the answer.
""".strip()

_GENERIC_PREAMBLE = re.compile(
    r"^\s*(?:(?:certainly|absolutely|of course|sure)[!,.]?\s*|"
    r"(?:great|good|excellent) question[!,.]?\s*)+",
    re.IGNORECASE,
)
_INTERNAL_LINE = re.compile(
    r"(?im)^.*(?:subprocess|traceback|exit code|stderr|"
    r"provider executable|provider subprocess|claude code cli|codex cli|"
    r"system[_ ]context|permission prompt wasn't granted|"
    r"provided workspace is .*read-only|mount or attach .*repository).*$"
)
_WINDOWS_PATH = re.compile(r"(?<![\w])(?:[A-Za-z]:\\(?:[^\s<>|\"']+\\)*[^\s<>|\"']*)")
_UNIX_PATH = re.compile(r"(?<!\w)/(?:[^\s/]+/)+[^\s,.;:!?]+")
_REPO_PATH = re.compile(r"(?i)[A-Za-z]:\\TARS(?:-[^\s<>|\"']*)?(?:\\[^\s<>|\"']*)?")
_RAW_URL = re.compile(r"https?://[^\s)>]+|www\.[^\s)>]+", re.IGNORECASE)
_MARKDOWN_LINK = re.compile(r"!?\[([^\]]*)\]\((?:[^()]|\([^)]*\))*\)")
_FENCED_CODE = re.compile(r"```(?:[^\n]*)\n?[\s\S]*?```", re.MULTILINE)
_TABLE_DIVIDER = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")
_SHORT_REQUEST = re.compile(
    r"\b(short|brief|briefly|concise|one sentence|two sentences|in a sentence|"
    r"answer only|just the answer|yes or no)\b",
    re.IGNORECASE,
)
_COMPLEX_REQUEST = re.compile(
    r"\b(compare|design|propose|analy[sz]e|debug|tradeoffs?|step[- ]by[- ]step|"
    r"root causes?|acceptance plan|multi[- ]part|sections?|explain.*(?:and|then))\b",
    re.IGNORECASE,
)
_UNCERTAIN_REQUEST = re.compile(
    r"\b(no .*provided|not provided|without (?:data|evidence|context)|"
    r"insufficient|incomplete|unknown|do not have|don't have|no current chart|"
    r"no .*feed|no .*state|not given)\b",
    re.IGNORECASE,
)
_UNCERTAINTY_LANGUAGE = re.compile(
    r"\b(can(?:not|'t)|do not have|don't have|not enough|insufficient|unknown|"
    r"not provided|incomplete|unavailable|need (?:the|more)|no validated trade)\b",
    re.IGNORECASE,
)
_UNSUPPORTED_CERTAINTY = re.compile(
    r"\b(?:guaranteed|definitely|certainly)\b|\b\d{1,3}\s*%\s*(?:confidence|certain)",
    re.IGNORECASE,
)
_MULTIPART = re.compile(r"\b(?:and|also|plus|then|versus|vs\.?|compare)\b", re.IGNORECASE)
_STRUCTURE = re.compile(r"(?m)^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|[A-Z][^\n:]{0,30}:\s*)")


def classify_complexity(user_text: str) -> ResponseComplexity:
    text = user_text.strip()
    if _COMPLEX_REQUEST.search(text) or len(text.split()) >= 35:
        return ResponseComplexity.COMPLEX
    if _SHORT_REQUEST.search(text) or len(text.split()) <= 7:
        return ResponseComplexity.SHORT
    return ResponseComplexity.ORDINARY


def sanitize_display_text(text: str) -> str:
    """Remove filler and diagnostic leakage while retaining useful Markdown."""

    cleaned = _GENERIC_PREAMBLE.sub("", text.strip())
    cleaned = _INTERNAL_LINE.sub("", cleaned)
    cleaned = _REPO_PATH.sub("the local workspace", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned or "I couldn't produce a useful answer."


def prepare_speech_text(display_text: str, *, max_chars: int = 900) -> str:
    """Create bounded natural speech with no Markdown, tables, URLs, or paths."""

    text = display_text.strip()
    text = _FENCED_CODE.sub(" A code example is included on screen. ", text)
    text = _MARKDOWN_LINK.sub(lambda match: match.group(1), text)
    text = _RAW_URL.sub(_spoken_url, text)
    text = _WINDOWS_PATH.sub("", text)
    text = _UNIX_PATH.sub("", text)

    spoken_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or _TABLE_DIVIDER.match(line):
            continue
        if "|" in line and line.count("|") >= 2:
            cells = [cell.strip() for cell in line.strip("|").split("|") if cell.strip()]
            line = ". ".join(cells)
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", line)
        line = re.sub(r"`([^`]*)`", r"\1", line)
        line = re.sub(r"\*\*([^*]+)\*\*|__([^_]+)__", lambda m: m.group(1) or m.group(2), line)
        line = re.sub(r"\*([^*]+)\*|_([^_]+)_", lambda m: m.group(1) or m.group(2), line)
        line = re.sub(r"[*#`~|]", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            spoken_lines.append(line)

    speech = " ".join(spoken_lines)
    speech = re.sub(r"\s+([,.;:!?])", r"\1", speech)
    speech = re.sub(r"([.!?])\s*([A-Z])", r"\1 \2", speech).strip()
    if len(speech) > max_chars:
        speech = speech[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:") + "."
    return speech


def _spoken_url(match: re.Match[str]) -> str:
    value = match.group(0)
    host = re.sub(r"^(?:https?://|www\.)", "", value, flags=re.IGNORECASE)
    host = host.split("/", 1)[0].rstrip(".,;:!?")
    return host


def public_error_message(kind: str = "assistant") -> str:
    if kind == "stt":
        return "I couldn't understand that audio. Please try again."
    if kind == "tts":
        return "I couldn't prepare the spoken response. Please try again."
    if kind == "chart":
        return "I couldn't analyze that chart right now. Please try again."
    return "I couldn't answer that right now. Please try again."


class ResponseQualityContract:
    """Evaluate observable response qualities without model-based judging."""

    def assess(
        self,
        *,
        user_text: str,
        display_text: str,
        speech_text: str,
        grounding_context: str = "",
    ) -> ResponseQualityAssessment:
        complexity = classify_complexity(user_text)
        words = len(display_text.split())
        directness = bool(display_text.strip()) and not bool(_GENERIC_PREAMBLE.match(display_text))
        if complexity is ResponseComplexity.SHORT:
            directness = directness and words <= 80
        elif complexity is ResponseComplexity.COMPLEX:
            directness = directness and words <= 550
        else:
            directness = directness and words <= 300

        asks_multiple = bool(_MULTIPART.search(user_text))
        completeness = bool(display_text.strip()) and not display_text.rstrip().endswith(("...", ":"))
        if complexity is ResponseComplexity.COMPLEX and asks_multiple:
            completeness = completeness and words >= 45

        unsupported_certainty = bool(_UNSUPPORTED_CERTAINTY.search(display_text))
        has_grounding = bool(grounding_context.strip())
        grounding = not unsupported_certainty
        if "quant_brain" in user_text.lower() or "trade" in user_text.lower():
            grounding = grounding and (
                has_grounding
                or bool(_UNCERTAINTY_LANGUAGE.search(display_text))
                or "NO VALIDATED TRADE" in display_text
            )

        uncertainty_needed = bool(_UNCERTAIN_REQUEST.search(user_text))
        uncertainty = not uncertainty_needed or bool(_UNCERTAINTY_LANGUAGE.search(display_text))
        structure = complexity is not ResponseComplexity.COMPLEX or bool(_STRUCTURE.search(display_text))
        user_clean = not bool(_INTERNAL_LINE.search(display_text)) and not bool(
            _WINDOWS_PATH.search(display_text)
        )
        speech_clean = bool(speech_text.strip()) and not any(
            marker in speech_text for marker in ("**", "*", "###", "`", "|")
        )
        speech_clean = speech_clean and not bool(_RAW_URL.search(speech_text))
        speech_clean = speech_clean and not bool(_WINDOWS_PATH.search(speech_text))

        flags = {
            "directness": directness,
            "completeness": completeness,
            "grounding": grounding,
            "uncertainty": uncertainty,
            "structure": structure,
            "user_mode_cleanliness": user_clean,
            "speech_suitability": speech_clean,
        }
        return ResponseQualityAssessment(
            **flags,
            issues=tuple(name for name, passed in flags.items() if not passed),
        )


class ResponseComposer:
    def __init__(self, contract: ResponseQualityContract | None = None) -> None:
        self._contract = contract or ResponseQualityContract()

    def compose(
        self,
        *,
        user_text: str,
        display_text: str,
        grounding_context: str = "",
    ) -> ResponsePresentation:
        display = sanitize_display_text(display_text)
        speech = prepare_speech_text(display)
        quality = self._contract.assess(
            user_text=user_text,
            display_text=display,
            speech_text=speech,
            grounding_context=grounding_context,
        )
        return ResponsePresentation(display_text=display, speech_text=speech, quality=quality)
