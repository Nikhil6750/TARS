# CURRENT_STATE.md — TARS Current State

Keep this file current. Whoever lands a meaningful change updates this file
in the same change (coordinator-authorized edit — this file is otherwise
shared/read-only, see [AGENTS.md](../../AGENTS.md)).

## Stage

**Bootstrap.** Coordination docs and contracts exist. No backend or
frontend application code has been written yet.

## Branches

- `main` — no commits yet (unborn at bootstrap time). Not yet used as a
  trunk; a coordinator will decide when/how `integration/v1` merges back.
- `integration/v1` — active branch. Holds this bootstrap commit
  (coordination docs, contracts, repo scaffolding only).

## What exists

- `AGENTS.md` — agent entry point and ownership rules.
- `docs/coordination/` — `MASTER_SPEC.md`, `ARCHITECTURE.md`,
  `CURRENT_STATE.md` (this file), `DECISIONS.md`, `TASKS.md`, and
  `handoffs/{claude,codex,antigravity}.md`.
- `contracts/trading-event.schema.json` — v1.0.0, frozen pending real usage.
- `contracts/assistant-message.schema.json` — v1.0.0, frozen pending real
  usage.
- `.env.example`, `.gitignore`, `README.md`.

## What does not exist yet

- `backend/` — no FastAPI app, no WebSocket server, no SQLite models, no
  voice provider implementations, no mock trading-event generator.
- `frontend/` — no React/Vite app.
- `tests/` — no integration/contract-verification harness.
- No CI configuration.
- No `.env` (only `.env.example`).

## Known blockers / open questions

- No coordinator-assigned tasks yet — `TASKS.md` lists proposed next steps
  but implementation has not been authorized to begin.
- Wake-word model for "TARS" does not exist yet; push-to-talk fallback is
  the only guaranteed activation path until one is built and validated
  (see ADR-006 in `DECISIONS.md`).

## Next recommended action

Coordinator reviews this bootstrap (`AGENTS.md`, `MASTER_SPEC.md`,
`ARCHITECTURE.md`, `DECISIONS.md`, contracts) and assigns first-wave tasks
in `TASKS.md`, at which point Claude Code, Codex, and Antigravity begin
parallel implementation each within their owned directories.
