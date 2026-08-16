# CURRENT_STATE.md — TARS Current State

Keep this file current. Whoever lands a meaningful change updates this file
in the same change (coordinator-authorized edit — this file is otherwise
shared/read-only, see [AGENTS.md](../../AGENTS.md)).

## Stage

**Wave 1 Integration & End-to-End Verification (COMPLETE).**
All three Wave 1 branches (`feature/v1-backend-voice`, `feature/v1-quality-contracts`,
`feature/v1-web-pwa`) were cleanly integrated into `feature/v1-integration` on the dedicated
`C:\TARS-Integration` worktree without squashing, preserving full commit histories.
Integration reconciliation, API/WebSocket route aliasing, 1 MiB payload protection,
assistant grounding enhancements, and deterministic event lifecycle persistence have been
implemented and verified end-to-end against all contract, unit, and black-box acceptance suites.

## Branches & Workspaces

- `main` — mirrors the bootstrap baseline commit. Untouched.
- `integration/v1` — integration trunk holding the merged architecture baseline.
- `feature/v1-backend-voice` — Claude Code backend/voice/memory implementation (SHA: `5f5a1fb`).
- `feature/v1-quality-contracts` — Codex contracts/acceptance harness (SHA: `2c67932`).
- `feature/v1-web-pwa` — Antigravity React/Tauri/PWA foundation (SHA: `5d677db`).
- `feature/v1-integration` — Unified integrated application branch at `C:\TARS-Integration`.

## Verification Status

| Test Suite | Total | Passed | Skipped | Status |
|---|---|---|---|---|
| **Codex Acceptance Suite** (`tools/run_acceptance.py`) | 15 | 15 | 0 | **ALL PASSED** |
| **Backend Unit & Integration Tests** (`apps/backend/tests/`) | 62 | 61 | 1 | **ALL PASSED** (1 skipped clean without extras) |
| **Contract & Fixture Tests** (`tests/`) | 55 | 40 | 15 | **ALL PASSED** (15 acceptance run via harness) |
| **Contract Schema Codegen Check** (`tools/generate_contracts.py --check`) | 2 | 2 | 0 | **CLEAN (0 drift)** |
| **Frontend Vitest Suite** (`apps/web/`) | 12 | 12 | 0 | **ALL PASSED** |
| **Frontend TypeScript Build** (`tsc && vite build`) | 1 | 1 | 0 | **SUCCESS (PWA & dist built)** |

## What exists

- `apps/backend/` — FastAPI REST + WebSocket application, SQLite state/history + FTS5 indexing, voice pipeline (openWakeWord, Silero VAD, faster-whisper, Kokoro/Fish Speech/Mock), deterministic assistant router + memory grounding, 1 MiB body limit middleware.
- `apps/web/` — React 19 + TypeScript + Vite PWA + Tauri 2 desktop shell, real-time WebSocket client with strict contract schema validation, Companion face UI, Active Setups view, Alert history, Memory/Research view with SQLite FTS5 search, Ask TARS assistant chat view, Voice control with PTT, and Settings.
- `contracts/` — Frozen canonical schemas: `trading-event.schema.json` (v1.0.0), `assistant-message.schema.json` (v1.0.0).
- `tests/`, `tools/` — Contract test fixtures (6 valid, 7 malformed), codegen tooling, black-box acceptance runner (`tools/run_acceptance.py`), test client (`tools/tars_test_client.py`), Playwright UI viewport tests (desktop & iPhone 390x844).
- `docs/coordination/handoffs/integration.md` — Integration handoff report.

## Next recommended action

Codex independently inspects and certifies `feature/v1-integration`.
Coordinator prepares merge of `feature/v1-integration` into `integration/v1`.
