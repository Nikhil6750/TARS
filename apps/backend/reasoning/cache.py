"""Cache for IMMUTABLE research answers only.

An answer is cacheable iff EVERY evidence item is flagged immutable (e.g. a terminal experiment whose spec hash + data version are in the
provenance), the answer was independently reviewed (never UNREVIEWED), and the key pins the question, the exact evidence content, the review
model and the prompt/version. Anything involving market quotes, news, charts or a non-terminal experiment is never cached. A changed spec,
data version, result or model produces a different key, so a stale answer can never be served for different evidence."""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from .review import SYSTEM_PROMPT, EvidencePacket

CACHE_VERSION = 1
_PROMPT_SHA = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:12]


def normalize_question(q: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", q.lower())).strip()


def cache_key(question: str, evidence: list, model: str) -> str | None:
    if not evidence or not all(getattr(e, "immutable", False) for e in evidence):
        return None
    body = json.dumps([[e.name, e.kind, e.as_of, e.provenance, e.summary] for e in sorted(evidence, key=lambda x: x.name)], sort_keys=True)
    return hashlib.sha256(f"{CACHE_VERSION}|{_PROMPT_SHA}|{model}|{normalize_question(question)}|{body}".encode()).hexdigest()[:32]


class ResearchAnswerCache:
    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)
        self.hits = self.misses = self.writes = 0

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def get(self, key: str | None) -> dict | None:
        if key is None:
            return None
        try:
            entry = json.loads(self._path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.misses += 1
            return None
        if entry.get("version") != CACHE_VERSION or not entry.get("independently_reviewed"):
            self.misses += 1
            return None
        self.hits += 1
        return entry

    def put(self, key: str | None, packet: EvidencePacket, decision, review) -> bool:
        """Store only independently reviewed answers for immutable evidence."""
        if key is None or not decision.independently_reviewed or review is None or review.verdict.value == "ERROR":
            return False
        self.dir.mkdir(parents=True, exist_ok=True)
        entry = {"version": CACHE_VERSION, "stored_at": time.time(), "key": key, "question": packet.question, "experiment_id": packet.experiment_id,
                 "status": decision.status.value, "spoken": decision.spoken, "detail": decision.detail, "flags": decision.flags, "reasons": decision.reasons,
                 "independently_reviewed": True, "review_verdict": review.verdict.value, "review_model": review.model, "claude_text": packet.claude_analysis,
                 "record": decision.record}
        tmp = self._path(key).with_suffix(".tmp")
        tmp.write_text(json.dumps(entry), encoding="utf-8")
        tmp.replace(self._path(key))
        self.writes += 1
        return True
