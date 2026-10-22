"""Intelligence pipeline: route -> (local | Gemini | evidence -> Claude -> ChatGPT review -> decision) with truthful streaming.

Rules enforced here:
  * simple actions never touch a model
  * evidence collectors run in PARALLEL with per-collector timeouts (a slow collector degrades to UNAVAILABLE, never blocks the answer)
  * Claude is called once; the reviewer is called once (review + wording in one call); no duplicate model calls
  * NOTHING about a trading conclusion is yielded before the decision engine has run - only an honest ACK and progress markers
  * a cancel event (barge-in) stops work at the next stage boundary and suppresses the FINAL event"""
from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator, Awaitable, Callable

from .cache import ResearchAnswerCache, cache_key
from .decision import Decision, DecisionEngine
from .review import EvidenceItem, EvidencePacket, ReviewResult, TradingReviewService
from .routing import IntentRiskRouter, Route, RouteDecision

PATH_OF = {Route.SIMPLE_ACTION: "FAST", Route.CHAT: "CONVERSATION", Route.TRADING_QUESTION: "RESEARCH", Route.RESEARCH_REQUEST: "RESEARCH"}

Collector = Callable[[str], Awaitable[EvidenceItem]]
ClaudeFn = Callable[[EvidencePacket], Awaitable[dict]]            # -> {"text": str, "claims_edge": bool, "assumptions": [...], "limitations": [...]}
LocalExec = Callable[[object], Awaitable[str]]
ChatStream = Callable[[str], AsyncIterator[str]]


@dataclass
class Event:
    kind: str      # ACK | PROGRESS | DELTA | FINAL | CANCELLED | ERROR
    text: str = ""
    t: float = 0.0
    data: dict = field(default_factory=dict)


class LatencyBook:
    def __init__(self) -> None:
        self.samples: dict[str, list[float]] = {}

    def add(self, stage: str, seconds: float) -> None:
        self.samples.setdefault(stage, []).append(seconds)

    def summary(self) -> dict:
        out = {}
        for k, v in self.samples.items():
            s = sorted(v)
            out[k] = {"n": len(s), "p50": round(statistics.median(s), 4), "p95": round(s[min(len(s) - 1, int(0.95 * len(s)))], 4), "max": round(s[-1], 4)}
        return out


class IntelligencePipeline:
    def __init__(self, *, router: IntentRiskRouter | None = None, local_exec: LocalExec | None = None, chat: ChatStream | None = None,
                 collectors: dict[str, Collector] | None = None, claude: ClaudeFn | None = None, reviewer: TradingReviewService | None = None,
                 engine: DecisionEngine | None = None, collector_timeout_s: float = 4.0, claude_timeout_s: float = 90.0, store_decision=None,
                 cache: ResearchAnswerCache | None = None, cache_model: str | None = None) -> None:
        self.router = router or IntentRiskRouter()
        self.local_exec, self.chat, self.collectors = local_exec, chat, collectors or {}
        self.claude, self.reviewer, self.engine = claude, reviewer, engine or DecisionEngine()
        self.collector_timeout_s, self.claude_timeout_s = collector_timeout_s, claude_timeout_s
        self.latency = LatencyBook()
        self.store_decision = store_decision
        self.cache, self.cache_model = cache, cache_model
        self.last_decision: Decision | None = None
        self.calls = {"claude": 0, "review": 0, "gemini": 0, "local": 0, "cache_hit": 0}

    async def _collect(self, question: str) -> list[EvidenceItem]:
        async def one(name: str, fn: Collector) -> EvidenceItem | None:
            try:
                return await asyncio.wait_for(fn(question), timeout=self.collector_timeout_s)
            except Exception as exc:       # degrade, never block, never invent
                return EvidenceItem(name=name, freshness="UNAVAILABLE", summary=f"collector failed: {type(exc).__name__}")
        results = await asyncio.gather(*(one(n, f) for n, f in self.collectors.items()))
        flat: list[EvidenceItem] = []
        for r in results:
            if r is None:
                continue
            flat.extend(r if isinstance(r, list) else [r])
        return flat

    async def stream(self, text: str, cancel: asyncio.Event | None = None, *, instrument: str | None = None, timeframe: str | None = None) -> AsyncIterator[Event]:
        t0 = time.perf_counter()
        now = lambda: time.perf_counter() - t0
        cancelled = lambda: cancel is not None and cancel.is_set()
        rd: RouteDecision = self.router.route(text)
        self.latency.add("router", rd.router_seconds)

        if rd.route is Route.SIMPLE_ACTION:
            self.calls["local"] += 1
            t1 = time.perf_counter()
            result = await self.local_exec(rd.local_intent) if self.local_exec else "Done."
            self.latency.add("local_command", time.perf_counter() - t1)
            self.latency.add("first_safe_output", now())
            self.latency.add("end_to_end", now())
            yield Event("FINAL", result or "Done.", now(), {"route": rd.route.value, "path": "FAST", "models": []})
            return

        if rd.route is Route.CHAT:
            self.calls["gemini"] += 1
            first = None
            async for chunk in self.chat(text):
                if cancelled():
                    yield Event("CANCELLED", "", now())
                    return
                if first is None:
                    first = now()
                    self.latency.add("first_safe_output", first)
                yield Event("DELTA", chunk, now())
            self.latency.add("end_to_end", now())
            yield Event("FINAL", "", now(), {"route": rd.route.value, "path": "CONVERSATION", "models": ["conversation"]})
            return

        # ---- trading / research: honest acknowledgement first, conclusion only after the decision engine ----------------
        self.latency.add("first_safe_output", now())
        yield Event("ACK", "Checking the evidence and getting an independent review.", now(), {"route": rd.route.value})
        t1 = time.perf_counter()
        evidence = await self._collect(text)
        self.latency.add("evidence", time.perf_counter() - t1)
        if cancelled():
            yield Event("CANCELLED", "", now())
            return
        yield Event("PROGRESS", f"Collected {sum(e.freshness == 'CURRENT' for e in evidence)}/{len(evidence)} current evidence sources.", now())
        ckey = cache_key(text, evidence, self.cache_model or "") if self.cache is not None else None
        hit = self.cache.get(ckey) if ckey else None
        if hit is not None:         # identical immutable evidence, same model/prompt: the reviewed answer cannot have changed
            self.calls["cache_hit"] += 1
            self.latency.add("cache_hit", now())
            self.latency.add("end_to_end", now())
            yield Event("FINAL", hit["spoken"], now(), {"route": rd.route.value, "path": "RESEARCH", "status": hit["status"], "detail": hit["detail"], "flags": hit["flags"] + ["CACHED"],
                                                        "independently_reviewed": True, "cached": True, "cached_at": hit["stored_at"], "review_model": hit["review_model"]})
            return

        packet = EvidencePacket(question=text[:600], instrument=instrument, timeframe=timeframe, evidence=evidence, consequential=True)
        analysis = {"text": "", "claims_edge": False, "assumptions": [], "limitations": []}
        if self.claude is not None:
            t1 = time.perf_counter()
            self.calls["claude"] += 1
            try:
                analysis = await asyncio.wait_for(self.claude(packet), timeout=self.claude_timeout_s)
            except Exception as exc:
                analysis = {"text": "", "claims_edge": False, "assumptions": [], "limitations": [f"primary analysis unavailable: {type(exc).__name__}"]}
            self.latency.add("claude", time.perf_counter() - t1)
        if cancelled():
            yield Event("CANCELLED", "", now())
            return
        packet = packet.model_copy(update={"claude_analysis": analysis.get("text", "")[:3500], "claude_claims_edge": bool(analysis.get("claims_edge")),
                                           "assumptions": analysis.get("assumptions", []), "limitations": analysis.get("limitations", []),
                                           "quant": analysis.get("quant"), "experiment_id": analysis.get("experiment_id")})
        yield Event("PROGRESS", "Primary analysis done; independent review running.", now())
        review: ReviewResult | None = None
        if rd.needs_independent_review and self.reviewer is not None:
            t1 = time.perf_counter()
            self.calls["review"] += 1
            review = await self.reviewer.review(packet, cancel=cancel)
            self.latency.add("review", time.perf_counter() - t1)
        if cancelled():
            yield Event("CANCELLED", "", now())
            return
        t1 = time.perf_counter()
        decision = self.engine.decide(packet, review)
        self.latency.add("decision", time.perf_counter() - t1)
        self.last_decision = decision
        if self.cache is not None and ckey:
            self.cache.put(ckey, packet, decision, review)
        if self.store_decision:
            self.store_decision(decision, packet, review)
        self.latency.add("end_to_end", now())
        yield Event("FINAL", decision.spoken, now(), {"route": rd.route.value, "path": "RESEARCH", "status": decision.status.value, "detail": decision.detail,
                                                      "flags": decision.flags, "independently_reviewed": decision.independently_reviewed,
                                                      "review_model": getattr(review, "model", None), "review_fallback_from": getattr(review, "fallback_from", None)})
