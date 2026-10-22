"""Production assembly of the TARS intelligence paths.

  FAST          local commands, no model, executed by the caller-supplied executor
  CONVERSATION  one fast model (default gpt-5.6-luna, effort low) with fallbacks and a truthful failure line
  RESEARCH      evidence (parallel) -> Claude -> ChatGPT independent review (with fallback models) -> deterministic decision;
                immutable, already-reviewed answers are served from cache

Mandatory review is enforced inside the pipeline: a consequential answer whose review fails is labelled UNREVIEWED, never silently trusted."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import AsyncIterator

from research.claude_adapter import ClaudeResearchLLM

from . import siwc
from .cache import ResearchAnswerCache
from .collectors import detect_instrument, experiment_collector, public_quote, upcoming_events
from .conversation import ConversationService, openai_candidate
from .decision import DecisionEngine
from .openai_client import OpenAIResponsesClient
from .pipeline import Event, IntelligencePipeline
from .review import EvidencePacket, TradingReviewService, select_review_model

CONVERSATION_MODELS = ("gpt-5.6-luna", "gpt-6-luna", "gpt-5.6-terra")
REVIEW_FALLBACKS = ("gpt-6-luna", "gpt-5.6-terra")

CLAUDE_SYSTEM = (
    "You are the lead quantitative researcher assisting a personal trading assistant. Use ONLY the evidence given: label what is observed versus guessed, "
    "never invent prices, events or statistics, say plainly what is missing, and never present a chart pattern as having proven predictive edge. "
    'Reply with ONE JSON object: {"text": "<=100 words, plain spoken language", "claims_edge": true|false} (claims_edge is true only if you assert a statistically '
    "validated trading edge)."
)


class IntelligenceService:
    def __init__(self, *, openai_client: OpenAIResponsesClient, review_model: str, claude_llm: ClaudeResearchLLM | None = None, registry=None, agent=None,
                 cache_dir: Path | None = None, conversation_models=CONVERSATION_MODELS, fallback_models=REVIEW_FALLBACKS, local_exec=None,
                 reviewer_effort: str | None = None, review_timeout_s: float = 60.0, quote_fn=public_quote, calendar_fn=upcoming_events) -> None:
        self.client, self.review_model = openai_client, review_model
        self.claude_llm = claude_llm or ClaudeResearchLLM(max_calls=10_000)
        self.registry, self.agent = registry, agent
        self.quote_fn, self.calendar_fn = quote_fn, calendar_fn
        self.cache = ResearchAnswerCache(cache_dir) if cache_dir else None
        self.conversation = ConversationService([openai_candidate(openai_client, m) for m in conversation_models])
        self.reviewer = TradingReviewService(openai_client, review_model, timeout_s=review_timeout_s, fallback_models=tuple(m for m in fallback_models if m != review_model),
                                             reasoning_effort=reviewer_effort)
        self._instrument: str | None = None
        collectors = {"quote": self._quote, "calendar": self._calendar}
        if registry is not None and agent is not None:
            collectors["experiment"] = experiment_collector(registry, agent)
        self.pipeline = IntelligencePipeline(local_exec=local_exec, chat=self.conversation.stream, collectors=collectors, claude=self._claude,
                                             reviewer=self.reviewer, engine=DecisionEngine(), cache=self.cache, cache_model=review_model, collector_timeout_s=10.0,
                                             claude_timeout_s=120.0)

    def add_collector(self, name: str, fn) -> None:
        self.pipeline.collectors[name] = fn

    async def review_existing(self, question: str, answer: str, evidence: list, *, instrument: str | None = None, timeframe: str | None = None,
                              claims_edge: bool = False, limitations: list[str] | None = None, cancel=None):
        """Independent review + deterministic decision for an answer another component already produced (e.g. the chart explainer).
        Returns (Decision, ReviewResult|None). Never raises: a failed review comes back as UNREVIEWED."""
        packet = EvidencePacket(question=question[:600], instrument=instrument, timeframe=timeframe, evidence=evidence, claude_analysis=answer[:3500],
                                claude_claims_edge=claims_edge, limitations=limitations or [], consequential=True)
        try:
            review = await self.reviewer.review(packet, cancel=cancel)
        except Exception:
            review = None
        return self.pipeline.engine.decide(packet, review), review

    # ---- evidence collectors (all return None when not applicable) -------------------------------------------------------------
    async def _quote(self, question: str):
        sym = self._instrument or detect_instrument(question)
        if sym is None or re.search(r"\bH\d{4}\b", question or ""):
            return None
        return await self.quote_fn(sym)

    async def _calendar(self, question: str):
        sym = self._instrument or detect_instrument(question)
        if sym is None or re.search(r"\bH\d{4}\b", question or ""):
            return None
        return await self.calendar_fn("USD")

    # ---- primary analysis -------------------------------------------------------------------------------------------------------------
    async def _claude(self, packet: EvidencePacket) -> dict:
        lines = "\n".join(f"- [{e.freshness}] {e.name} (as of {e.as_of or 'unknown'}): {e.summary}" for e in packet.evidence) or "- (no evidence could be collected)"
        raw = await self.claude_llm(CLAUDE_SYSTEM, f"QUESTION: {packet.question}\nEVIDENCE:\n{lines}")
        m = re.search(r"\{.*\}", raw, re.S)
        obj = json.loads(m.group(0))
        out = {"text": str(obj["text"]), "claims_edge": bool(obj.get("claims_edge")),
               "assumptions": ["evidence limited to the items listed"], "limitations": [e.summary for e in packet.evidence if e.freshness != "CURRENT"][:3]}
        exp = next((e for e in packet.evidence if e.kind == "quant"), None)
        if exp is not None and self.registry is not None and self.agent is not None:
            from .evidence import experiment_evidence
            hid = exp.name.removeprefix("quant_agent_")
            _item, quant = experiment_evidence(self.registry, self.agent, hid)
            out["quant"], out["experiment_id"] = quant, hid
        return out

    @staticmethod
    def resolve_instrument(value: str | None) -> str | None:
        """'gold' / 'XAU' / 'xauusd' / 'XAU/USD' -> XAUUSD; a six-letter pair stays as typed; anything unrecognised -> None (never guess)."""
        if not value:
            return None
        v = value.strip()
        hit = detect_instrument(v)
        if hit:
            return hit
        compact = re.sub(r"[^A-Za-z]", "", v).upper()
        return compact if len(compact) == 6 else None

    async def stream(self, text: str, cancel=None, *, instrument: str | None = None, timeframe: str | None = None) -> AsyncIterator[Event]:
        self._instrument = self.resolve_instrument(instrument) or detect_instrument(text)
        async for ev in self.pipeline.stream(text, cancel, instrument=self._instrument or instrument, timeframe=timeframe):
            yield ev

    @classmethod
    async def from_environment(cls, *, data_root: str = "D:/TARS_DATA", local_exec=None, with_registry: bool = True) -> "IntelligenceService":
        """Real wiring: stored SIWC credentials, account-discovered review model, Claude CLI, the research registry and the immutable-answer cache."""
        client = OpenAIResponsesClient(siwc.SiwcAuth(), timeout=90)
        model = await select_review_model(client)
        effort = os.environ.get("TARS_REVIEW_EFFORT") or None
        registry = agent = None
        if with_registry and Path(data_root, "experiments", "registry.sqlite").exists():
            from research.agent import QuantResearchAgent
            from research.registry import ExperimentRegistry
            from research.storage import MarketStore
            registry = ExperimentRegistry(Path(data_root) / "experiments" / "registry.sqlite")
            agent = QuantResearchAgent(registry, MarketStore(data_root), Path(data_root) / "reports")
        return cls(openai_client=client, review_model=model, registry=registry, agent=agent, cache_dir=Path(data_root) / "cache" / "research_answers", reviewer_effort=effort,
                   claude_llm=ClaudeResearchLLM(max_calls=10_000, log_path=Path(data_root) / "cache" / "claude_research_calls.jsonl"), local_exec=local_exec)
