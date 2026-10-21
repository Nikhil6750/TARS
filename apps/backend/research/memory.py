"""Research memory: before a hypothesis is accepted, search the registry for equivalent/similar work and refuse
to repeat rejected research unless the new hypothesis is materially different."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .registry import ExperimentRegistry
from .spec import ResearchSpec

_STOP = {"the", "a", "an", "of", "to", "and", "or", "in", "on", "is", "are", "be", "that", "this", "for", "with", "by",
         "it", "as", "at", "from", "when", "price", "market", "than", "more", "less"}
DEAD_STATUSES = {"REJECTED", "FAILED"}
SIMILARITY_THRESHOLD = 0.6
SAME_MECHANISM_THRESHOLD = 0.8


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP and len(t) > 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _mech_text(mech: dict) -> str:
    return " ".join(str(v) for v in mech.values())


@dataclass
class MemoryMatch:
    experiment_id: str
    status: str
    kind: str  # EQUIVALENT | SIMILAR
    similarity: float
    mechanism_similarity: float


@dataclass
class MemoryVerdict:
    allowed: bool
    reason: str
    matches: list[MemoryMatch] = field(default_factory=list)


class ResearchMemory:
    def __init__(self, registry: ExperimentRegistry) -> None:
        self.registry = registry

    def check(self, spec: ResearchSpec, *, exclude: str | None = None) -> MemoryVerdict:
        mine = _tokens(spec.hypothesis + " " + _mech_text(spec.mechanism.model_dump()))
        my_mech = _tokens(_mech_text(spec.mechanism.model_dump()))
        fp = spec.fingerprint()
        matches: list[MemoryMatch] = []
        for row in self.registry.list():
            if row["id"] == exclude or row["diagnostic"]:
                continue  # pipeline diagnostics are not research claims and never block real hypotheses
            full = self.registry.get(row["id"])
            theirs = _tokens(full["hypothesis"] + " " + _mech_text(full["mechanism"]))
            their_mech = _tokens(_mech_text(full["mechanism"]))
            sim = _jaccard(mine, theirs)
            msim = _jaccard(my_mech, their_mech)
            same_inst = bool(set(spec.instruments) & set(row["instruments"].split(",")))
            if row["fingerprint"] == fp and same_inst:
                matches.append(MemoryMatch(row["id"], row["status"], "EQUIVALENT", sim, msim))
            elif same_inst and sim >= SIMILARITY_THRESHOLD:
                matches.append(MemoryMatch(row["id"], row["status"], "SIMILAR", sim, msim))
        for m in matches:
            declared = m.experiment_id in spec.differs_from
            if m.status in DEAD_STATUSES:
                if m.kind == "EQUIVALENT" and (not declared or m.mechanism_similarity >= SAME_MECHANISM_THRESHOLD):
                    return MemoryVerdict(False, f"{m.experiment_id} ({m.status}) tested an equivalent rule set with a "
                                         f"{'near-identical' if declared else 'declared-unrelated'} mechanism; do not repeat "
                                         "rejected research without a materially different mechanism", matches)
                if m.kind == "SIMILAR" and not declared:
                    return MemoryVerdict(False, f"{m.experiment_id} ({m.status}) is a similar hypothesis; declare it in "
                                         "differs_from with a substantive differs_because or choose another idea", matches)
            elif m.kind == "EQUIVALENT":
                return MemoryVerdict(False, f"{m.experiment_id} ({m.status}) already covers this rule set; continue that "
                                     "experiment instead of duplicating it", matches)
        return MemoryVerdict(True, "no blocking equivalent research found" if not matches else "similar work exists but is "
                             "either live or explicitly differentiated", matches)
