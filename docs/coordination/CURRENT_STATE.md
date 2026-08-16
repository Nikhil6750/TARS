# CURRENT_STATE.md — TARS Current State

Keep this file current. Whoever lands a meaningful change updates this file
in the same change (coordinator-authorized edit — this file is otherwise
shared/read-only, see [AGENTS.md](../../AGENTS.md)).

## Stage

**Wave 1 Implementation (Active).** Architecture amendment
(`feature/architecture-free-stack`) has been merged into `integration/v1`.
Parallel isolated worktrees and feature branches have been created and
authorized for Wave 1 implementation across Claude Code, Codex, and
Antigravity.

## Branches & Workspaces

- `main` — mirrors the bootstrap baseline commit. Untouched. Not used for
  feature work.
- `integration/v1` — integration trunk holding the merged architecture
  baseline and active coordination authorization.
- `feature/v1-backend-voice` — active feature branch for Claude Code at
  `C:\TARS-Claude`.
- `feature/v1-quality-contracts` — active feature branch for Codex at
  `C:\TARS-Codex`.
- `feature/v1-web-pwa` — active feature branch for Antigravity at
  `C:\TARS-Antigravity`.

## Authorized Agent Scopes (Wave 1)

| Agent | Worktree Path | Branch | Owns | Wave 1 Scope |
|---|---|---|---|---|
| **Claude Code** | `C:\TARS-Claude` | `feature/v1-backend-voice` | `apps/backend/` | Backend (FastAPI, WebSockets, SQLite), voice pipeline (openWakeWord, Silero VAD, faster-whisper, Fish Speech/Kokoro), assistant routing (ClaudeCodeProvider, OllamaProvider), memory backend (FTS5), mock trading events |
| **Codex** | `C:\TARS-Codex` | `feature/v1-quality-contracts` | `tests/`, `tools/` | Contracts/code-generation, contract verification harness, WebSocket integration tests, acceptance verification for free/local runtime |
| **Antigravity** | `C:\TARS-Antigravity` | `feature/v1-web-pwa` | `apps/web/` | Single React + TS + Vite codebase, Tauri 2 desktop shell, responsive PWA, companion face/state UI, active setups & alert history views, browser/E2E verification |

### Shared Rules
- `contracts/` remains **read-only** for all agents.
- `docs/coordination/*.md` (except own handoff) remains **read-only**.
- Each agent updates **only its own** handoff file under `docs/coordination/handoffs/`.
- No features implemented directly on `main` or `integration/v1`.

## What exists

- `AGENTS.md` — agent entry point, ownership rules, and the permanent Git
  workflow rule.
- `docs/coordination/` — `MASTER_SPEC.md`, `ARCHITECTURE.md` (amended
  stack), `CURRENT_STATE.md` (this file), `DECISIONS.md` (ADR-001 through
  ADR-020), `TASKS.md`, and `handoffs/{claude,codex,antigravity}.md`.
- `contracts/trading-event.schema.json` — v1.0.0, frozen, read-only.
- `contracts/assistant-message.schema.json` — v1.0.0, frozen, read-only.
- `.env.example` — local-first stack configuration template.
- `.gitignore`, `README.md`.
- Isolated worktrees configured at `C:\TARS-Claude`, `C:\TARS-Codex`, and
  `C:\TARS-Antigravity`.

## What does not exist yet

- `apps/backend/` — application code to be created by Claude Code in Wave 1.
- `apps/web/` — frontend codebase to be created by Antigravity in Wave 1.
- `tests/`, `tools/` — verification suites and test harnesses to be created
  by Codex in Wave 1.
- ESP32 firmware — explicitly out of scope for Wave 1.

## Known blockers / open questions

- None. Workspaces and branches are isolated and verified.

## Next recommended action

Agents proceed with Wave 1 implementation inside their respective isolated
worktrees on their assigned feature branches. Update individual handoff files
upon completing logical units.
