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
| **Claude Code** | `apps/backend/` | FastAPI backend, WebSocket event server, SQLite state/history + FTS5/semantic search, voice provider adapters (`WakeWordProvider`, `SpeechToTextProvider`, `AssistantProvider`, `TextToSpeechProvider`) and the Pipecat voice orchestration pipeline, assistant routing (`ClaudeCodeProvider` / `OllamaProvider` / optional `AnthropicAPIProvider`), mock trading-event generator |
| **Codex** | `tests/`, `tools/` | Contract and acceptance verification — validates all producers/consumers against `contracts/*.schema.json`, integration tests across the backend/frontend boundary, CI checks |
| **Antigravity** | `apps/web/` | Single React + TypeScript + Vite codebase, wrapped by Tauri 2 for desktop and shipped as an installable/responsive PWA for iPhone — do not create two independent UI codebases. Companion state/face UI, browser/E2E verification |

See [ARCHITECTURE.md](docs/coordination/ARCHITECTURE.md) for what each
directory contains and how the pieces fit together.

### Shared, read-only after this amendment

- `contracts/` — the event/message schemas. Backend, frontend, tests, and the
  future quant_brain/ESP32 integrations all depend on these. **Do not modify
  without explicit coordinator sign-off.** If your work seems to require a
  contract change, stop and record the need in your handoff instead of
  editing the schema directly.
- `docs/coordination/*.md` (except your own handoff file) — read-only.
- `AGENTS.md` itself — read-only.

Shared contracts and architecture stay read-only during parallel
implementation. If you hit a real blocker that requires changing one,
escalate it (record the blocker in your handoff and stop) rather than
editing it yourself.

## Git workflow (permanent rule)

This applies to every agent, every session, from this point forward:

- **Never** implement features directly on `main`.
- **Never** implement features directly on `integration/v1`, except
  explicit integration work (merging already-reviewed feature branches,
  resolving integration-level conflicts, or a change a coordinator
  explicitly designates as integration work).
- Every feature, phase, meaningful sub-feature, or architectural change gets
  its own feature branch, branched from `integration/v1` (e.g.
  `feature/<short-description>`).
- Each completed logical unit gets one meaningful commit — messages explain
  *why*, not just what changed.
- Push commits to GitHub after the logical unit passes its relevant
  validation (schema validation, tests, lint — whatever applies to that
  unit).
- Do not create empty/noise commits merely to increase commit count.
- Do not squash development history when eventually merging
  `integration/v1` into `main`, unless the user explicitly requests it.
- Do not modify git `user.name` or `user.email`.
- Never fabricate authorship — a commit must reflect who/what actually made
  the change.
- Every handoff entry must contain both the branch name and the commit SHA
  (this is already part of the handoff template below — treat it as
  mandatory, not optional).

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
- **The core TARS runtime must work without any paid API.** Wake word
  (openWakeWord), VAD (Silero), STT (faster-whisper), TTS (Fish Speech,
  falling back to Kokoro), and the assistant (`OllamaProvider` or
  `ClaudeCodeProvider` against the user's own authenticated Claude Code
  environment) all have a fully local/free path. Paid providers
  (`AnthropicAPIProvider`, hosted Fish Audio, hosted STT, etc.) exist only
  as optional adapters behind the existing provider interfaces — never a
  requirement. Do not claim every optional provider is free; only the
  required core path must be.
- Voice architecture is provider-neutral. Every external service (STT,
  assistant, TTS, wake word) must have a mock/local fallback so the app runs
  with zero API keys. Retain the `WakeWordProvider`, `SpeechToTextProvider`,
  `AssistantProvider`, `TextToSpeechProvider` interfaces — never hard-code a
  specific implementation into business logic.
- Trading facts must always come from deterministic TARS/quant_brain state,
  never from model invention. Do not force every TARS command through an
  LLM — deterministic command/state requests resolve in deterministic code.
- Memory text (conversation memory, journal entries, notes) must never be
  used as proof of trading performance. Validated trading intelligence
  comes from `quant_brain` only.
- No secrets in source control. Use `.env` (gitignored) based on
  `.env.example`. Never log secrets (see `ARCHITECTURE.md` § Observability).

## Current stage

Architecture amendment stage: the production stack was revised after a
deeper tool audit (see `docs/coordination/DECISIONS.md` ADR-010 onward) and
is being committed on branch `feature/architecture-free-stack`, branched
from `integration/v1`. This amends coordination docs and architecture only
— no backend or frontend application code has been implemented yet. Do not
begin feature implementation until a coordinator assigns tasks via
`TASKS.md` on a dedicated feature branch, per the Git workflow rule above.
