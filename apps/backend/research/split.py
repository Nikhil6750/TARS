"""Fixed temporal research split. Rule-based (calendar month aligned) and independent of how much data happens to be downloaded,
so it cannot be nudged after looking at results. Written ONCE to experiments/research_split.json; the MarketStore reads it and
refuses any read that reaches into FINAL_UNTOUCHED unless explicitly authorised."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


class SplitError(RuntimeError):
    pass


def _iso(t: pd.Timestamp) -> str:
    return t.tz_convert("UTC").isoformat()


@dataclass(frozen=True)
class ResearchSplit:
    instrument: str
    provider: str
    data_start: str
    data_end: str          # exclusive
    development: tuple[str, str]
    validation: tuple[str, str]
    final_untouched: tuple[str, str]
    rule: str

    def hash(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]


def propose_split(instrument: str, provider: str, data_start: str, data_end: str, *, final_months: int = 12,
                  validation_months: int = 15) -> ResearchSplit:
    """FINAL = the most recent `final_months`; VALIDATION = the `validation_months` before it; DEVELOPMENT = everything older."""
    s, e = pd.Timestamp(data_start, tz="UTC"), pd.Timestamp(data_end, tz="UTC")
    f0 = e - pd.DateOffset(months=final_months)
    v0 = f0 - pd.DateOffset(months=validation_months)
    if v0 <= s:
        raise SplitError("data range too short for the requested validation/final windows")
    return ResearchSplit(instrument.upper(), provider, _iso(s), _iso(e), (_iso(s), _iso(v0)), (_iso(v0), _iso(f0)),
                         (_iso(f0), _iso(e)), f"FINAL=last {final_months} months, VALIDATION=prior {validation_months} months, DEVELOPMENT=rest")


def write_split(root: Path, split: ResearchSplit) -> Path:
    """Create or extend research_split.json. An existing instrument entry is immutable: re-writing the identical split is a no-op,
    anything else is refused (the final window may never be moved after the fact)."""
    path = Path(root) / "experiments" / "research_split.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"instruments": {}}
    cur = doc["instruments"].get(split.instrument)
    entry = asdict(split)
    if cur is not None:
        if cur["hash"] != split.hash():
            raise SplitError(f"{split.instrument} already has a frozen split ({cur['hash']}); it cannot be changed")
        return path
    entry["hash"] = split.hash()
    entry["frozen_at"] = datetime.now(timezone.utc).isoformat()
    doc["instruments"][split.instrument] = entry
    path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return path


def load_split(root: Path, instrument: str) -> dict | None:
    path = Path(root) / "experiments" / "research_split.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))["instruments"].get(instrument.upper())
