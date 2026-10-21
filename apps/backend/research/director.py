"""Claude Research Director contract. Claude (lead quant researcher) receives COMPACT evidence packets and answers
with strictly-typed JSON; every conclusion carries exactly one ConclusionClass. The director code enforces the
epistemic rules itself: Claude cannot upgrade a conclusion beyond what the registry status supports.

The LLM is injected as `async (system, user) -> str` so any provider (Claude Code CLI, API, a test double) fits."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .agent import QuantResearchAgent, ResearchRejected
from .registry import ExperimentRegistry
from .spec import ResearchSpec, ResearchSpecError, parse_spec

LLM = Callable[[str, str], Awaitable[str]]
MAX_PACKET_CHARS = 16000

SYSTEM_PROMPT = (
    "You are the lead quantitative researcher for TARS. You formulate falsifiable hypotheses with an explicit market "
    "mechanism and produce structured ResearchSpecs; a deterministic Quant Agent runs every calculation. You never "
    "compute statistics yourself, never ask for parameter tuning until a result looks good, and never claim an edge "
    "from a backtest alone. Every conclusion must be classified as exactly one of: HYPOTHESIS, PRELIMINARY_EVIDENCE, "
    "VALIDATED_FINDING, UNSUPPORTED_CLAIM, FAILED_EXPERIMENT. Reply with a single JSON object only, exactly this shape: "
    '{"classification": "<one of the five classes>", "summary": "<1-3 sentences>", "reasoning": "<brief>", '
    '"decision": "REJECT|REFINE|VALIDATE_NEXT|NONE", "spec": <ResearchSpec object or null>, "next_steps": ["<short>"]}. '
    "No prose outside the JSON, no markdown fences, and use only the evidence given."
)


class ConclusionClass(str, Enum):
    HYPOTHESIS = "HYPOTHESIS"
    PRELIMINARY_EVIDENCE = "PRELIMINARY_EVIDENCE"
    VALIDATED_FINDING = "VALIDATED_FINDING"
    UNSUPPORTED_CLAIM = "UNSUPPORTED_CLAIM"
    FAILED_EXPERIMENT = "FAILED_EXPERIMENT"


class Decision(str, Enum):
    REJECT = "REJECT"
    REFINE = "REFINE"
    VALIDATE_NEXT = "VALIDATE_NEXT"
    NONE = "NONE"


class DirectorOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    classification: ConclusionClass
    summary: str = Field(min_length=3)
    reasoning: str = ""
    decision: Decision = Decision.NONE
    spec: dict | None = None
    next_steps: list[str] = Field(default_factory=list)


class DirectorError(RuntimeError):
    pass


@dataclass
class DirectorResult:
    task: str
    output: DirectorOutput
    downgraded_from: str | None = None
    spec: ResearchSpec | None = None
    experiment_id: str | None = None
    notes: list[str] = field(default_factory=list)


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise DirectorError("Claude reply contained no JSON object")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise DirectorError(f"Claude reply was not valid JSON: {exc}") from exc


def enforce_epistemics(cls: ConclusionClass, statuses: list[str]) -> tuple[ConclusionClass, str | None]:
    """Never let narrative outrun the registry. Returns (allowed class, reason if downgraded)."""
    if cls is ConclusionClass.VALIDATED_FINDING and (not statuses or any(s != "VALIDATED" for s in statuses)):
        return ConclusionClass.UNSUPPORTED_CLAIM, "VALIDATED_FINDING requires registry status VALIDATED"
    if cls is ConclusionClass.PRELIMINARY_EVIDENCE and statuses and all(s in {"FAILED", "REJECTED"} for s in statuses):
        return ConclusionClass.FAILED_EXPERIMENT, "experiment is FAILED/REJECTED in the registry"
    return cls, None


class ResearchDirector:
    def __init__(self, llm: LLM, registry: ExperimentRegistry, agent: QuantResearchAgent) -> None:
        self.llm, self.registry, self.agent = llm, registry, agent

    async def _ask(self, task: str, instruction: str, packet: dict | list | str) -> DirectorOutput:
        body = packet if isinstance(packet, str) else json.dumps(packet, default=str)
        if len(body) > MAX_PACKET_CHARS:
            raise DirectorError(f"evidence packet is {len(body)} chars (> {MAX_PACKET_CHARS}); send summaries, not raw data")
        prompt = f"TASK: {task}\n{instruction}\n\nEVIDENCE:\n{body}"
        last: Exception | None = None
        for attempt in range(2):  # one corrective retry for malformed output; never more
            extra = "" if attempt == 0 else f"\n\nYour previous reply was rejected ({last}). Reply with ONLY one JSON object matching the schema."
            raw = await self.llm(SYSTEM_PROMPT, prompt + extra)
            try:
                return DirectorOutput.model_validate(_extract_json(raw))
            except ValidationError as exc:
                last = DirectorError(f"schema validation: {exc.errors()[0]['msg']}")
            except DirectorError as exc:
                last = exc
        raise DirectorError(f"Claude reply unusable after retry: {last}")

    def _result(self, task: str, out: DirectorOutput, statuses: list[str], **kw) -> DirectorResult:
        cls, why = enforce_epistemics(out.classification, statuses)
        res = DirectorResult(task, out, downgraded_from=out.classification.value if why else None, **kw)
        if why:
            res.output = out.model_copy(update={"classification": cls})
            res.notes.append(why)
        return res

    # ---- the six director functions ---------------------------------------------------------------------
    async def propose_hypothesis(self, topic: str, context: str = "") -> DirectorResult:
        """Claude drafts a ResearchSpec; it is validated, checked against research memory, then frozen in the registry."""
        digest = [{"id": r["id"], "title": r["title"], "status": r["status"], "failure": r["failure_reason"]} for r in self.registry.list()][-40:]
        out = await self._ask("propose_hypothesis", "Return JSON {classification:'HYPOTHESIS', summary, reasoning, spec:<ResearchSpec>}. "
                              "Do not repeat REJECTED/FAILED work listed below unless the mechanism is materially different "
                              "(then fill differs_from/differs_because).", {"topic": topic, "context": context, "registry": digest})
        res = self._result("propose_hypothesis", out, [])
        if out.spec is None:
            raise DirectorError("propose_hypothesis returned no spec")
        try:
            spec = parse_spec(out.spec)
        except ResearchSpecError as exc:
            res.notes.append(f"SPEC_INVALID: {exc}")
            return res
        try:
            res.experiment_id = self.agent.submit(spec)
            res.spec = spec
        except ResearchRejected as exc:
            res.notes.append(f"MEMORY_REJECTED: {exc}")
        return res

    async def review_experiment(self, hid: str) -> DirectorResult:
        pkt = self.agent.evidence_packet(hid)
        out = await self._ask("review_experiment", "Review the evidence. Decide REJECT / REFINE / VALIDATE_NEXT and classify.", pkt)
        res = self._result("review_experiment", out, [pkt["status"]], experiment_id=hid)
        self.registry.add_review(hid, {"classification": res.output.classification.value, "decision": out.decision.value,
                                       "summary": out.summary, "downgraded_from": res.downgraded_from})
        return res

    async def diagnose_failure(self, hid: str) -> DirectorResult:
        pkt = self.agent.evidence_packet(hid)
        out = await self._ask("diagnose_failure", "Explain the most likely cause of failure using only the evidence.", pkt)
        return self._result("diagnose_failure", out, [pkt["status"]], experiment_id=hid)

    async def design_validation(self, hid: str) -> DirectorResult:
        pkt = self.agent.evidence_packet(hid)
        out = await self._ask("design_validation", "Design the next validation steps WITHOUT touching the untouched test period.", pkt)
        return self._result("design_validation", out, [pkt["status"]], experiment_id=hid)

    async def compare_experiments(self, ids: list[str]) -> DirectorResult:
        pkts = [self.agent.evidence_packet(i) for i in ids]
        slim = [{k: p[k] for k in ("experiment_id", "status", "classification", "development", "validation", "warnings", "failure_reason")} for p in pkts]
        out = await self._ask("compare_experiments", "Compare these experiments; note multiple-testing exposure.", slim)
        return self._result("compare_experiments", out, [p["status"] for p in pkts])

    async def recommend_next_experiment(self, context: str = "") -> DirectorResult:
        digest = [{"id": r["id"], "title": r["title"], "status": r["status"], "failure": r["failure_reason"]} for r in self.registry.list()][-40:]
        out = await self._ask("recommend_next_experiment", "Recommend ONE next experiment (as next_steps), avoiding dead ends.",
                              {"context": context, "registry": digest})
        return self._result("recommend_next_experiment", out, [])
