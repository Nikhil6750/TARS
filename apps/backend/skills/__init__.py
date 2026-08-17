"""Wave 2A Windows-wide skill execution layer.

Each module here implements one `BaseSkill` (see `app.action_contracts`)
that the action runtime (Codex's `apps/backend/actions/`) dispatches a
validated `ActionRequest` into. Skills are deterministic: `classify_risk()`
and `validate()` are plain code with no I/O and no LLM calls, per
`docs/coordination/wave2/M2A_SPEC.md`'s non-negotiables. `execute()` performs
the real action and returns a real `ActionResult` -- never a fabricated
`SUCCEEDED` status for something that did not actually happen.

See `skills/registry.py` for the assembled registry Codex's runtime imports.
"""
from __future__ import annotations
