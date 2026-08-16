# CURRENT_STATE.md — TARS Current State

Keep this file current. Whoever lands a meaningful change updates this file
in the same change (coordinator-authorized edit — this file is otherwise
shared/read-only, see [AGENTS.md](../../AGENTS.md)).

## Stage

**Architecture amendment (post-bootstrap, pre-implementation).** The
bootstrap was approved; a deeper tool audit then changed several
implementation choices (stack, voice, assistant providers, memory,
connectivity, notifications, scheduling, observability — see `DECISIONS.md`
ADR-010 onward). Coordination docs and architecture were amended to reflect
this. No backend or frontend application code has been written yet.

## Branches

- `main` — mirrors the bootstrap commit
  (`bd7b745927a35b7b2ae11d2facae3ac6a685e1e7`). Not yet used as a trunk for
  ongoing work; a coordinator will decide when/how `integration/v1` merges
  back. Per the Git workflow rule in `AGENTS.md`, features are never
  implemented directly on `main`.
- `integration/v1` — holds the approved bootstrap commit. Per the Git
  workflow rule, features are never implemented directly here either,
  except explicit integration work.
- `feature/architecture-free-stack` — active branch for this architecture
  amendment, branched from `integration/v1` at `bd7b745`. Coordination-doc
  and contract changes only; no application code.

## What exists

- `AGENTS.md` — agent entry point, ownership rules, and the permanent Git
  workflow rule.
- `docs/coordination/` — `MASTER_SPEC.md`, `ARCHITECTURE.md` (amended
  stack), `CURRENT_STATE.md` (this file), `DECISIONS.md` (ADR-001 through
  ADR-020), `TASKS.md`, and `handoffs/{claude,codex,antigravity}.md`.
- `contracts/trading-event.schema.json` — v1.0.0, frozen, **unchanged by
  the architecture amendment**.
- `contracts/assistant-message.schema.json` — v1.0.0, frozen, unchanged;
  its free-form `providers.*` fields already accommodate the new provider
  names (`claude_code`, `ollama`, `faster_whisper`, `fish_speech`, `kokoro`,
  `openwakeword`, etc.) with no schema edit needed.
- `.env.example` — amended for the local-first stack (Pipecat/openWakeWord/
  Silero/faster-whisper/Fish Speech-Kokoro, Ollama/Claude Code/optional
  Anthropic API, Tailscale, observability, scheduling).
- `.gitignore`, `README.md`.

## What does not exist yet

- `apps/backend/` — no FastAPI app, no WebSocket server, no SQLite models,
  no voice provider implementations, no mock trading-event generator, no
  Pipecat pipeline.
- `apps/web/` — no React/Vite app, no Tauri 2 shell.
- `tests/`, `tools/` — no integration/contract-verification harness.
- No CI configuration.
- No `.env` (only `.env.example`).
- ESP32 firmware — explicitly out of scope for this and the prior stage.

## Known blockers / open questions

- No coordinator-assigned tasks yet — `TASKS.md` lists proposed next steps
  (updated for the new stack) but implementation has not been authorized to
  begin.
- Wake-word model for "TARS" via openWakeWord does not exist yet;
  push-to-talk/keyboard activation is the only guaranteed activation path
  until one is built and validated (see ADR-006, reaffirmed by ADR-011).

## Next recommended action

Coordinator reviews this amendment (`AGENTS.md`, `ARCHITECTURE.md`,
`DECISIONS.md` ADR-010–ADR-020) and, once satisfied, assigns first-wave
tasks in `TASKS.md` on dedicated `feature/*` branches per the Git workflow
rule, at which point Claude Code, Codex, and Antigravity begin parallel
implementation each within their owned directories
(`apps/backend/`, `apps/web/`, `tests/`+`tools/`).
