"""Deterministic decision engine. Pure rules, no LLM, fully traceable.

Order of precedence (first match wins for STATUS; flags accumulate):
  1. no usable current evidence for a market question                      -> WITHHELD
  2. quant statistics contradict a claimed edge                            -> REJECTED_CLAIM
  3. mandatory independent review failed / missing                         -> UNREVIEWED (labelled; never 'independently verified')
  4. reviewer DISAGREE                                                     -> WITHHELD (both views disclosed)
  5. reviewer INSUFFICIENT_EVIDENCE                                        -> WITHHELD
  6. reviewer QUALIFY, stale data, or any limitation                       -> QUALIFIED
  7. reviewer APPROVE with clean evidence                                  -> APPROVED
Never forces Claude and the reviewer to agree and never invents numbers."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum

from .review import EvidencePacket, ReviewResult, Verdict, sanitize_spoken


class Status(str, Enum):
    APPROVED = "APPROVED"
    QUALIFIED = "QUALIFIED"
    WITHHELD = "WITHHELD"
    REJECTED_CLAIM = "REJECTED_CLAIM"
    UNREVIEWED = "UNREVIEWED"


@dataclass
class Decision:
    status: Status
    spoken: str                       # short, natural, safe to speak
    detail: str                       # expandable workspace text
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    independently_reviewed: bool = False
    record: dict = field(default_factory=dict)


def _first_sentences(text: str, n: int = 2, max_words: int = 55) -> str:
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    out = " ".join(parts[:n])
    words = out.split()
    return " ".join(words[:max_words]) + ("…" if len(words) > max_words else "")


def quant_sentence(q: dict | None) -> str | None:
    """One short, natural sentence built ONLY from deterministic Quant Agent numbers (never from model prose)."""
    if not q or q.get("status") is None:
        return None
    eid = q.get("experiment_id") or "The experiment"
    val, dev = q.get("validation_net_expectancy_r"), q.get("net_expectancy_r")
    if q["status"] in ("REJECTED", "FAILED"):
        bits = []
        if val is not None:
            bits.append(f"{val:+.2f}R per trade in validation")
        if dev is not None:
            bits.append(f"{dev:+.2f}R in development")
        tail = f": net expectancy was {' and '.join(bits)} after costs" if bits else ""
        gross = q.get("gross_expectancy_r")
        if gross is not None and abs(gross) < 0.03:
            tail += ", and before costs there was essentially no edge either"
        return f"{eid} didn't survive validation{tail}."
    if q["status"] == "VALIDATED":
        return f"{eid} passed its prespecified validation checks, which is a research result, not a trading signal."
    return f"{eid} is {str(q['status']).lower()}."


SPOKEN_MAX_WORDS = 42


def cap_spoken(text: str, max_words: int = SPOKEN_MAX_WORDS) -> tuple[str, bool]:
    """Short by default. If longer than the cap, keep whole leading sentences that fit and say the rest is on screen (uncertainty is never dropped
    silently: the full text always remains in the decision detail)."""
    words = (text or "").split()
    if len(words) <= max_words:
        return text, False
    out = ""
    for sent in re.split(r"(?<=[.!?])\s+", text.strip()):
        if len((out + " " + sent).split()) > max_words:
            break
        out = (out + " " + sent).strip()
    if not out:
        out = " ".join(words[:max_words]).rstrip(",;:") + "…"
    return out + " The caveats are in the details.", True


def _quant_contradicts(q: dict | None) -> str | None:
    if not q:
        return None
    ne = q.get("validation_net_expectancy_r", q.get("net_expectancy_r"))
    if q.get("status") in ("REJECTED", "FAILED"):
        return f"experiment {q.get('experiment_id', '')} status is {q.get('status')}".strip()
    if ne is not None and ne <= 0:
        return f"tested net expectancy is {ne:+.3f}R after costs"
    return None


class DecisionEngine:
    def __init__(self, now=lambda: datetime.now(timezone.utc)) -> None:
        self._now = now

    def decide(self, packet: EvidencePacket, review: ReviewResult | None) -> Decision:
        flags: list[str] = []
        reasons: list[str] = []
        market = [e for e in packet.evidence if e.kind in ("market", "chart", "watcher")]
        stale = [e for e in packet.evidence if e.freshness == "STALE"]
        unavailable = [e for e in packet.evidence if e.freshness == "UNAVAILABLE"]
        if stale:
            flags.append("STALE_EVIDENCE")
        if unavailable:
            flags.append("MISSING_EVIDENCE")
        if packet.limitations:
            flags.append("LIMITATIONS")
        newest_as_of = max((e.as_of for e in packet.evidence if e.as_of), default=None)

        def finish(status: Status, spoken: str, detail: str, reviewed: bool) -> Decision:
            full = sanitize_spoken(spoken)
            spoken, was_capped = cap_spoken(full)
            if was_capped:      # the complete wording (with every caveat) is never lost: it leads the expandable detail
                detail = "Full explanation: " + full + chr(10) * 2 + detail
            rec = {"status": status.value, "decided_at": self._now().isoformat(), "packet_digest": packet.digest(), "experiment_id": packet.experiment_id,
                   "question": packet.question, "instrument": packet.instrument, "timeframe": packet.timeframe, "evidence_as_of": newest_as_of,
                   "evidence_freshness": {e.name: e.freshness for e in packet.evidence}, "reasons": reasons, "flags": flags,
                   "review_verdict": review.verdict.value if review else None, "review_model": getattr(review, "model", None),
                   "review_error": review.error_kind if review and review.verdict is Verdict.ERROR else None,
                   "review_sha": hashlib.sha256(review.model_dump_json().encode()).hexdigest()[:16] if review else None,
                   "independently_reviewed": reviewed}
            return Decision(status, spoken, detail, list(reasons), list(flags), reviewed, rec)

        # 1. market questions need current evidence
        if packet.instrument and market and all(e.freshness != "CURRENT" for e in market):
            reasons.append("no current market evidence (all stale or unavailable)")
            when = f" The latest data I have is from {newest_as_of}." if newest_as_of else ""
            return finish(Status.WITHHELD, f"I don't have current {packet.instrument} data, so I won't call it.{when}",
                          "Evidence freshness: " + json.dumps({e.name: e.freshness for e in packet.evidence}), False)

        # 2. quant contradicts the claim
        contra = _quant_contradicts(packet.quant)
        if packet.claude_claims_edge and contra:
            reasons.append("claimed edge contradicted by Quant Agent: " + contra)
            flags.append("QUANT_CONTRADICTION")
            return finish(Status.REJECTED_CLAIM, f"The tested numbers don't support that: {contra}.",
                          f"Claim rejected by deterministic statistics ({contra}).", review is not None and review.verdict is not Verdict.ERROR)

        # 3. mandatory independent review
        if packet.consequential and (review is None or review.verdict is Verdict.ERROR):
            why = "no review attempted" if review is None else f"review failed ({review.error_kind})"
            reasons.append("independent review unavailable: " + why)
            flags.append("UNREVIEWED")
            qs = quant_sentence(packet.quant)
            if qs:      # deterministic facts stay speakable; only the INTERPRETATION is unreviewed
                spoken = f"{qs} I couldn't get an independent review of the interpretation, so treat that part as unverified."
            else:
                spoken = f"I couldn't get an independent check on this, so treat it as unverified. {_first_sentences(packet.claude_analysis, 1)}".strip()
            return finish(Status.UNREVIEWED, spoken,
                          f"UNREVIEWED: {why}.\n\nPrimary analysis (unverified):\n{packet.claude_analysis}", False)
        if review is None:      # non-consequential, no review needed
            return finish(Status.APPROVED, _first_sentences(packet.claude_analysis, 2), packet.claude_analysis, False)

        detail = _review_detail(review)
        # 4/5. disagreement or insufficient evidence
        if review.verdict is Verdict.DISAGREE:
            reasons.append("independent reviewer materially disagrees")
            flags.append("DISAGREEMENT")
            point = review.material_disagreements[0] if review.material_disagreements else "the key conclusion"
            return finish(Status.WITHHELD, f"My two checks disagree on {point.rstrip('.')}, so I'm not calling it.", detail, True)
        if review.verdict is Verdict.INSUFFICIENT_EVIDENCE:
            reasons.append("reviewer: insufficient evidence")
            flags.append("INSUFFICIENT_EVIDENCE")
            miss = review.missing_evidence[0] if review.missing_evidence else "the evidence is incomplete"
            return finish(Status.WITHHELD, f"There isn't enough evidence to say: {miss.rstrip('.')}.", detail, True)

        # 6/7. qualified vs approved
        base = review.proposed_final_explanation or _first_sentences(packet.claude_analysis, 2)
        qualified = (review.verdict is Verdict.QUALIFY or "STALE_EVIDENCE" in flags or bool(review.unsupported_claims)
                     or ("LIMITATIONS" in flags and bool(review.confidence_limitations)))
        if qualified:
            reasons.append("approved with qualifications")
            prefix = f"Based on data from {newest_as_of}, not live: " if "STALE_EVIDENCE" in flags and newest_as_of else ""
            return finish(Status.QUALIFIED, prefix + base, detail, True)
        reasons.append("independent review supports the conclusion")
        return finish(Status.APPROVED, base, detail, True)


def _review_detail(r: ReviewResult) -> str:
    rows = []
    for title, items in (("Supported", r.supported_facts), ("Unsupported claims", r.unsupported_claims), ("Disagreements", r.material_disagreements),
                         ("Missing evidence", r.missing_evidence), ("Risks", r.risk_observations), ("Confidence limits", r.confidence_limitations)):
        if items:
            rows.append(f"{title}:\n- " + "\n- ".join(items))
    return f"Independent review: {r.verdict.value}" + (f" ({r.model})" if r.model else "") + ("\n\n" + "\n\n".join(rows) if rows else "")
