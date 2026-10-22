"""Evidence packets and the independent ChatGPT TradingReviewService (review + final wording in ONE structured call).

The reviewer sees only the bounded packet below. It must not invent prices, news or backtest numbers; anything it cannot ground in the
packet belongs in `unsupported_claims` / `missing_evidence`. Failures never raise into the caller: they come back as verdict ERROR so
the decision engine can label the answer UNREVIEWED instead of silently claiming independent verification."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .openai_client import (OpenAIAuthError, OpenAICancelled, OpenAIError, OpenAINotEligible, OpenAIResponsesClient, OpenAIUnavailable, OpenAIUnsupported,
                            OpenAIUsageLimit)

MAX_PACKET_CHARS = 14000
Freshness = Literal["CURRENT", "STALE", "UNAVAILABLE"]


class Verdict(str, Enum):
    APPROVE = "APPROVE"
    QUALIFY = "QUALIFY"
    DISAGREE = "DISAGREE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    ERROR = "ERROR"


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    kind: Literal["market", "news", "event", "watcher", "quant", "chart", "other"] = "other"
    as_of: str | None = None                 # ISO UTC timestamp of the underlying data
    freshness: Freshness = "UNAVAILABLE"
    provenance: str = ""
    summary: str = Field("", max_length=900)


class EvidencePacket(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(max_length=600)
    instrument: str | None = None
    timeframe: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    consequential: bool = True
    evidence: list[EvidenceItem] = Field(default_factory=list)
    claude_analysis: str = Field("", max_length=3500)
    claude_claims_edge: bool = False
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    quant: dict | None = None                # compact Quant Agent summary (experiment id, net expectancy, trades, status...)
    experiment_id: str | None = None

    def compact_json(self) -> str:
        text = self.model_dump_json(exclude_none=True)
        if len(text) > MAX_PACKET_CHARS:
            raise ValueError(f"evidence packet is {len(text)} chars (> {MAX_PACKET_CHARS}); summarise instead of sending raw data")
        return text

    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json(exclude={"created_at"}).encode()).hexdigest()[:16]


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    verdict: Verdict
    supported_facts: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    material_disagreements: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    risk_observations: list[str] = Field(default_factory=list)
    confidence_limitations: list[str] = Field(default_factory=list)
    proposed_final_explanation: str = Field("", max_length=1200)
    error_kind: str | None = None            # set only when verdict == ERROR: auth | usage_limit | unavailable | invalid_output | cancelled
    error_detail: str | None = None
    model: str | None = None
    seconds: float | None = None


SYSTEM_PROMPT = (
    "You are an independent reviewer for a trading research assistant. You did NOT produce the analysis you are given. Examine ONLY the "
    "evidence packet. Check: (1) every claim is supported by the supplied evidence; (2) causal claims (why price moved) are not asserted without "
    "support; (3) data is not stale or described as current when it is not; (4) instrument and timeframe are consistent; (5) costs and statistics "
    "are represented accurately; (6) the answer is internally consistent; (7) uncertainty is not understated; (8) the trading conclusion is defensible. "
    "Never invent prices, news, or performance numbers, and never agree just to agree. Reply with ONE JSON object, no markdown: "
    '{"verdict":"APPROVE|QUALIFY|DISAGREE|INSUFFICIENT_EVIDENCE","supported_facts":[],"unsupported_claims":[],"material_disagreements":[],'
    '"missing_evidence":[],"risk_observations":[],"confidence_limitations":[],"proposed_final_explanation":"<=2 short natural spoken sentences, '
    'plain language, no numeric confidence percentages, all material uncertainty kept"}. Use APPROVE only if there are no unsupported claims.'
)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_PCT_CONFIDENCE = re.compile(r"\b\d{1,3}\s*%\s*(?:confiden\w*|certain\w*|sure|probab\w*|chance|likel\w*)|\b(?:confiden\w*|certain\w*)\s*(?:of|at|:)?\s*\d{1,3}\s*%", re.I)
_SOFTEN = {"guaranteed": "not guaranteed", "guarantee": "no guarantee", "certain": "likely", "certainly": "probably", "risk-free": "risky", "risk free": "risky",
           "will definitely": "may", "sure thing": "possibility", "can't lose": "can lose", "cant lose": "can lose"}
_OVERCONFIDENT = re.compile(r"\b(guaranteed?|certain(?:ly)?|can'?t lose|risk[- ]free|will definitely|sure thing)\b", re.I)


def sanitize_spoken(text: str) -> str:
    """No fake numeric confidence or absolute certainty in user-facing wording."""
    t = _PCT_CONFIDENCE.sub("", text or "")
    t = _OVERCONFIDENT.sub(lambda m: _SOFTEN.get(m.group(0).lower(), "possible"), t)
    return re.sub(r"\s{2,}", " ", t).strip(" ,;")


def parse_review(raw: str) -> ReviewResult:
    text = _FENCE.sub("", raw.strip())
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("no JSON object in reviewer reply")
    obj = json.loads(m.group(0))
    res = ReviewResult.model_validate(obj)
    if res.verdict is Verdict.ERROR:
        raise ValueError("reviewer may not emit ERROR")
    if res.verdict is Verdict.APPROVE and res.unsupported_claims:       # enforce the rule the prompt states
        res = res.model_copy(update={"verdict": Verdict.QUALIFY})
    return res.model_copy(update={"proposed_final_explanation": sanitize_spoken(res.proposed_final_explanation)})


class TradingReviewService:
    """`llm` is async (system, user, cancel) -> (text, meta). Default: the OpenAI Responses client."""

    def __init__(self, client: OpenAIResponsesClient | None = None, model: str | None = None, *, timeout_s: float = 25.0,
                 llm: Callable[[str, str, asyncio.Event | None], Awaitable[tuple[str, dict]]] | None = None) -> None:
        if client is None and llm is None:
            raise ValueError("TradingReviewService needs an OpenAI client or an llm callable")
        self.client, self.model, self.timeout_s, self._llm = client, model, timeout_s, llm
        self.calls = 0

    async def _call(self, system: str, user: str, cancel: asyncio.Event | None) -> tuple[str, dict]:
        if self._llm is not None:
            return await self._llm(system, user, cancel)
        if not self.model:
            raise OpenAIUnavailable("no OpenAI model selected (run model discovery)")
        c = await self.client.complete(self.model, system, user, max_output_tokens=900, cancel=cancel, deadline=self.timeout_s)
        return c.text, {"model": c.model, "seconds": c.seconds, "first_token_seconds": c.first_token_seconds, "usage": c.usage}

    async def review(self, packet: EvidencePacket, *, cancel: asyncio.Event | None = None) -> ReviewResult:
        try:
            body = packet.compact_json()
        except ValueError as exc:
            return ReviewResult(verdict=Verdict.ERROR, error_kind="invalid_output", error_detail=str(exc))
        self.calls += 1
        try:
            text, meta = await asyncio.wait_for(self._call(SYSTEM_PROMPT, "EVIDENCE PACKET:\n" + body, cancel), timeout=self.timeout_s + 2)
            res = parse_review(text)
            return res.model_copy(update={"model": meta.get("model"), "seconds": meta.get("seconds")})
        except OpenAIAuthError as exc:
            return _err("auth", exc)
        except OpenAIUsageLimit as exc:
            return _err("usage_limit", exc)
        except OpenAINotEligible as exc:
            return _err("not_eligible", exc)
        except OpenAIUnsupported as exc:
            return _err("unsupported", exc)
        except OpenAICancelled as exc:
            return _err("cancelled", exc)
        except (OpenAIUnavailable, asyncio.TimeoutError, OpenAIError) as exc:
            return _err("unavailable", exc)
        except (ValueError, ValidationError, json.JSONDecodeError) as exc:
            return ReviewResult(verdict=Verdict.ERROR, error_kind="invalid_output", error_detail=f"{type(exc).__name__}: {str(exc)[:160]}")


def _err(kind: str, exc: BaseException) -> ReviewResult:
    return ReviewResult(verdict=Verdict.ERROR, error_kind=kind, error_detail=(str(exc) or type(exc).__name__)[:200])
