# AGENTS.md — TARS Coordination Entry Point

This file is the entry point for **every** agent session (Claude Code, Codex,
Antigravity, or any future agent) working in this repository. Read it first,
every time, before touching any code.

TARS is an AI-powered quantitative trading **companion** — not an autonomous
trading bot. It never places trades. See
[docs/coordination/MASTER_SPEC.md](docs/coordination/MASTER_SPEC.md) for the
full product spec and
[docs/coordination/ARCHITECTURE.md](docs/coordination/ARCHITECTURE.md) for
the technical architecture.

## Session start protocol (read in this order)

1. `AGENTS.md` (this file).
2. `docs/coordination/CURRENT_STATE.md` — what exists right now.
3. `docs/coordination/TASKS.md` — what is assigned and open.
4. `docs/coordination/DECISIONS.md` — decisions already made; do not re-litigate.
5. Your own handoff file under `docs/coordination/handoffs/` (see below).
6. Only the section(s) of `ARCHITECTURE.md` relevant to your task.
7. Only the source directories relevant to your ownership (see below).

Do **not** recursively re-read the entire repository. Use targeted
`git diff`, `git log -p -- <path>`, and `rg` searches scoped to what you
actually need. Do not dump large files into context unless required.

## Agent ownership

Parallel work begins after the bootstrap stage. Each agent owns a distinct
set of source directories. No two agents own the same directory. This is
what prevents merge conflicts — respect it even under pressure to "just fix"
something outside your lane; flag it in your handoff instead.

| Agent | Owns | Responsibility |
|---|---|---|
| **Claude Code** | `backend/` | FastAPI backend, WebSocket event server, SQLite state/history, voice provider adapters (`WakeWordProvider`, `SpeechToTextProvider`, `AssistantProvider`, `TextToSpeechProvider`), Claude/Anthropic integration, mock trading-event generator |
| **Codex** | `tests/`, `scripts/` (contract verification + integration/quality harness) | Validates all producers/consumers against `contracts/*.schema.json`, CI checks, automated integration tests across backend/frontend boundaries |
| **Antigravity** | `frontend/` | React + TypeScript + Vite responsive/PWA UI, laptop + iPhone experience, companion state/face UI, browser-side verification |

### Shared, read-only during parallel implementation

- `contracts/` — the event/message schemas. Backend, frontend, tests, and the
  future quant_brain/ESP32 integrations all depend on these. **Do not modify
  without explicit coordinator sign-off.** If your work seems to require a
  contract change, stop and record the need in your handoff instead of
  editing the schema directly.
- `docs/coordination/*.md` (except your own handoff file) — read-only.
- `AGENTS.md` itself — read-only.

### Handoff files

Each agent updates **only its own** file:

- `docs/coordination/handoffs/claude.md`
- `docs/coordination/handoffs/codex.md`
- `docs/coordination/handoffs/antigravity.md`

Every handoff update must contain:

- branch
- commit SHA
- work completed
- files changed
- interfaces exposed
- tests run
- known limitations
- exact dependencies required from other agents
- next recommended action

## Non-negotiable architectural boundaries

- TARS is a thin companion/interface. It must **never** duplicate
  quant_brain's backtesting, cost modelling, walk-forward validation,
  DSR/statistical validation, or strategy research database.
- No trading execution, ever.
- No fabricated strategy performance and no fake AI confidence percentages
  anywhere in the trading-event contract or UI.
- V1 uses **mock** trading events. quant_brain integration is a later stage
  and arrives only as a contract-compatible event source.
- Voice architecture is provider-neutral. Every external service (STT,
  assistant, TTS, wake word) must have a mock/local fallback so the app runs
  with zero API keys.
- No secrets in source control. Use `.env` (gitignored) based on
  `.env.example`.

## Current stage

This is the **bootstrap stage**: coordination docs and contracts only, on
branch `integration/v1`. No backend or frontend application code has been
implemented yet. Do not begin implementation until a coordinator assigns
tasks via `TASKS.md`.
