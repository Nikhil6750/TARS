"""Permanent experiment registry (SQLite; small operational state). IDs H0001.. are assigned atomically and are
immutable; the frozen spec, its hash and creation time cannot be updated and rows can never be deleted
(enforced by triggers). Failed experiments are kept forever."""
from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .spec import ResearchSpec

STATUSES = ("PROPOSED", "RUNNING", "FAILED", "PRELIMINARY", "VALIDATING", "VALIDATED", "REJECTED")
TRANSITIONS = {
    "PROPOSED": {"RUNNING", "REJECTED"},
    "RUNNING": {"FAILED", "PRELIMINARY", "REJECTED", "VALIDATING"},
    "FAILED": set(),
    "PRELIMINARY": {"VALIDATING", "REJECTED", "RUNNING"},
    "VALIDATING": {"VALIDATED", "REJECTED", "FAILED"},
    "VALIDATED": set(),
    "REJECTED": set(),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT NOT NULL UNIQUE,
  spec_json TEXT NOT NULL,
  spec_hash TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  title TEXT NOT NULL,
  instruments TEXT NOT NULL,
  hypothesis TEXT NOT NULL,
  mechanism_json TEXT NOT NULL,
  status TEXT NOT NULL,
  data_version TEXT,
  cost_assumptions_json TEXT,
  git_version TEXT,
  results_json TEXT,
  regime_results_json TEXT,
  warnings_json TEXT,
  quality_json TEXT,
  failure_reason TEXT,
  claude_review_json TEXT NOT NULL DEFAULT '[]',
  test_consumed INTEGER NOT NULL DEFAULT 0,
  diagnostic INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS experiment_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  experiment_id TEXT NOT NULL, at TEXT NOT NULL, event TEXT NOT NULL, detail TEXT
);
CREATE TRIGGER IF NOT EXISTS experiments_no_delete BEFORE DELETE ON experiments
BEGIN SELECT RAISE(ABORT, 'experiments are permanent and cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS experiments_frozen BEFORE UPDATE OF id, seq, spec_json, spec_hash, fingerprint, created_at ON experiments
BEGIN SELECT RAISE(ABORT, 'experiment definition is frozen'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON experiment_events
BEGIN SELECT RAISE(ABORT, 'experiment history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON experiment_events
BEGIN SELECT RAISE(ABORT, 'experiment history is append-only'); END;
"""


class RegistryError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def git_version() -> str:
    try:
        repo = Path(__file__).resolve().parents[3]
        sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--", "apps/backend/research"],
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return (sha or "unknown") + ("+dirty" if dirty else "")
    except Exception:
        return "unknown"


class ExperimentRegistry:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            cols = {r[1] for r in c.execute("PRAGMA table_info(experiments)")}
            if "final_first_accessed_at" not in cols:  # forward migration for registries created before Phase 3B
                c.execute("ALTER TABLE experiments ADD COLUMN final_first_accessed_at TEXT")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    # ---- create ---------------------------------------------------------------------------------
    def register(self, spec: ResearchSpec) -> str:
        """Freeze `spec` under a new permanent ID with status PROPOSED."""
        now = _now()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            n = (c.execute("SELECT COALESCE(MAX(seq),0) FROM experiments").fetchone()[0]) + 1
            hid = f"H{n:04d}"
            c.execute(
                "INSERT INTO experiments (id,spec_json,spec_hash,fingerprint,title,instruments,hypothesis,mechanism_json,status,"
                "cost_assumptions_json,git_version,diagnostic,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (hid, spec.canonical_json(), spec.spec_hash(), spec.fingerprint(), spec.title, ",".join(spec.instruments),
                 spec.hypothesis, spec.mechanism.model_dump_json(), "PROPOSED", spec.costs.model_dump_json(),
                 git_version(), int(spec.diagnostic), now, now))
            c.execute("INSERT INTO experiment_events (experiment_id,at,event,detail) VALUES (?,?,?,?)", (hid, now, "REGISTERED", spec.spec_hash()))
        return hid

    # ---- read -----------------------------------------------------------------------------------
    def get(self, hid: str) -> dict:
        with self._conn() as c:
            row = c.execute("SELECT * FROM experiments WHERE id=?", (hid,)).fetchone()
        if row is None:
            raise RegistryError(f"unknown experiment {hid}")
        d = dict(row)
        for k in ("results_json", "regime_results_json", "warnings_json", "quality_json", "claude_review_json",
                  "mechanism_json", "cost_assumptions_json"):
            d[k[:-5]] = json.loads(d[k]) if d.get(k) else None
        return d

    def spec(self, hid: str) -> ResearchSpec:
        return ResearchSpec.model_validate_json(self.get(hid)["spec_json"])

    def list(self, status: str | None = None) -> list[dict]:
        with self._conn() as c:
            q = "SELECT id,title,status,fingerprint,instruments,hypothesis,failure_reason,created_at,diagnostic FROM experiments"
            rows = c.execute(q + (" WHERE status=?" if status else "") + " ORDER BY seq", (status,) if status else ()).fetchall()
        return [dict(r) for r in rows]

    def history(self, hid: str) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT at,event,detail FROM experiment_events WHERE experiment_id=? ORDER BY id", (hid,))]

    # ---- state ----------------------------------------------------------------------------------
    def transition(self, hid: str, new: str, detail: str = "") -> None:
        cur = self.get(hid)["status"]
        if new not in STATUSES:
            raise RegistryError(f"unknown status {new}")
        if new not in TRANSITIONS[cur]:
            raise RegistryError(f"illegal transition {cur} -> {new} for {hid}")
        now = _now()
        with self._conn() as c:
            c.execute("UPDATE experiments SET status=?, updated_at=? WHERE id=?", (new, now, hid))
            c.execute("INSERT INTO experiment_events (experiment_id,at,event,detail) VALUES (?,?,?,?)", (hid, now, f"STATUS:{cur}->{new}", detail[:2000]))

    def record(self, hid: str, **fields) -> None:
        """Update mutable result columns only (definition columns are trigger-protected)."""
        allowed = {"data_version", "results_json", "regime_results_json", "warnings_json", "quality_json", "failure_reason"}
        sets, vals = [], []
        for k, v in fields.items():
            col = k if k.endswith("_json") or k in allowed else k
            if col not in allowed:
                raise RegistryError(f"column {k} is not recordable")
            sets.append(f"{col}=?")
            vals.append(v if isinstance(v, (str, type(None))) else json.dumps(v, default=str))
        if sets:
            with self._conn() as c:
                c.execute(f"UPDATE experiments SET {','.join(sets)}, updated_at=? WHERE id=?", (*vals, _now(), hid))

    def consume_test(self, hid: str) -> None:
        """The untouched test may be evaluated exactly once, ever."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT test_consumed FROM experiments WHERE id=?", (hid,)).fetchone()[0]:
                raise RegistryError(f"untouched test for {hid} was already consumed")
            now = _now()
            c.execute("UPDATE experiments SET test_consumed=1, updated_at=? WHERE id=?", (now, hid))
            c.execute("INSERT INTO experiment_events (experiment_id,at,event,detail) VALUES (?,?,?,?)", (hid, now, "TEST_CONSUMED", ""))

    def add_review(self, hid: str, review: dict) -> None:
        cur = self.get(hid)["claude_review"] or []
        cur.append({**review, "at": _now()})
        with self._conn() as c:
            c.execute("UPDATE experiments SET claude_review_json=?, updated_at=? WHERE id=?", (json.dumps(cur, default=str), _now(), hid))

    def trials_in_family(self, instruments: list[str], exclude: str | None = None) -> int:
        """Prior non-diagnostic experiments on overlapping instruments (multiple-testing denominator)."""
        want = set(instruments)
        n = 0
        for row in self.list():
            if row["id"] == exclude or row["diagnostic"]:
                continue
            if want & set(row["instruments"].split(",")):
                n += 1
        return n

    # ---- FINAL_UNTOUCHED access tracking -------------------------------------------------------------
    def mark_final_access(self, hid: str) -> bool:
        """Record the FIRST time this experiment's final period was loaded. Returns True if this call was the first."""
        now = _now()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute("SELECT final_first_accessed_at FROM experiments WHERE id=?", (hid,)).fetchone()[0]
            if cur:
                return False
            c.execute("UPDATE experiments SET final_first_accessed_at=?, updated_at=? WHERE id=?", (now, now, hid))
            c.execute("INSERT INTO experiment_events (experiment_id,at,event,detail) VALUES (?,?,?,?)", (hid, now, "FINAL_ACCESSED", ""))
        return True

    def final_access_conflicts(self, spec: "ResearchSpec", exclude: str | None = None) -> list[dict]:
        """Other experiments that already loaded a final period overlapping this spec's on a shared instrument:
        that data can no longer be called untouched for this spec."""
        out = []
        t = spec.untouched_test
        with self._conn() as c:
            rows = c.execute("SELECT id, final_first_accessed_at, spec_json FROM experiments WHERE final_first_accessed_at IS NOT NULL").fetchall()
        for r in rows:
            if r["id"] == exclude:
                continue
            other = ResearchSpec.model_validate_json(r["spec_json"])
            if set(other.instruments) & set(spec.instruments) and other.untouched_test.start < t.end and t.start < other.untouched_test.end:
                out.append({"experiment_id": r["id"], "first_accessed_at": r["final_first_accessed_at"]})
        return out

    def annotate(self, hid: str, note: str) -> None:
        """Append-only note on an experiment's history (never edits the frozen definition or results)."""
        self.get(hid)
        with self._conn() as c:
            c.execute("INSERT INTO experiment_events (experiment_id,at,event,detail) VALUES (?,?,?,?)", (hid, _now(), "NOTE", note[:2000]))
