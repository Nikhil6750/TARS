# CURRENT_STATE.md — TARS Current State

Keep this file current. Whoever lands a meaningful change updates this file
in the same change (coordinator-authorized edit — this file is otherwise
shared/read-only, see [AGENTS.md](../../AGENTS.md)).

## Stage

**OVERNIGHT TARS INTEGRATION (COMPLETE).**
Three parallel overnight streams -- `feature/overnight-tars-core` (TARS
Orchestrator, Trading Intelligence foundation, bounded Agent framework,
memory/performance work), `feature/overnight-voice-native` (voice-first
floating panel, local VAD/wake pipeline, native window handling), and
`feature/overnight-agent-runtime` (a durable, provider-neutral LLM-decision-
loop job runtime) -- merged into `feature/overnight-integration` via three
explicit `--no-ff` merges, preserving full unsquashed history. Pushed to
`origin/feature/overnight-integration`. Not yet merged into `integration/v1`
or `main`.

The tars-core and agent-runtime streams both independently built an
"agents" concept at `apps/backend/agents/` with real collisions (files,
routes, an app.state slot name, a test filename). Resolved by renaming
agent-runtime's package to `apps/backend/agent_runtime/` (router at
`/api/v1/agent-runtime`, `app.state.agent_job_runtime`) since the two are
genuinely different systems, not duplicates -- see
`docs/coordination/overnight/claude.done.json` for the full reconciliation
detail and final validation results (backend: 403 pytest passed, ruff/mypy
clean; frontend: 118 vitest passed, TypeScript build clean).

**WAVE 2A (M2A) RELEASE INTEGRATION (COMPLETE).**
Certified Wave 2A candidate `bbfd4903af5edb59b453baf9f844de73aad78d09`
(`feature/wave2-m2a-integration` — the merged and integration-fixed
combination of Claude's core skills, Codex's action runtime, and
Antigravity's native shell) merged into `integration/wave2` via an
explicit `--no-ff` merge commit `5c70fc8f308d0589e2e7dae88b3575ded46ddebb`,
preserving full unsquashed history. Pushed to `origin/integration/wave2`.
`integration/v1` and `main` intentionally not touched. Known non-blocking
verification gaps (tray-icon click, autostart-toggle UI, native
notification, full live-microphone PTT capture, wake word) recorded in
ADR-022 in `DECISIONS.md`.

**V1 RELEASE INTEGRATION (COMPLETE).**
Certified V1 candidate `fe8f787ab6a1565ad8e1f3b6cbacc5ef6a4bd1ee`
(`feature/v1-final-candidate-2`) merged into `integration/v1` via an
explicit `--no-ff` merge commit `f25566ac34aef1868ee09ee826d5ef82fc407aec`,
preserving full unsquashed history (no rebase/squash). Pushed to
`origin/integration/v1`. `main` intentionally not touched — that merge is
a separate, later coordinator decision. See ADR-021 in `DECISIONS.md`.

**Wave 1 Integration & End-to-End Verification (COMPLETE).**
All three Wave 1 branches (`feature/v1-backend-voice`, `feature/v1-quality-contracts`,
`feature/v1-web-pwa`) were cleanly integrated into `feature/v1-integration` on the dedicated
`C:\TARS-Integration` worktree without squashing, preserving full commit histories.
Integration reconciliation, API/WebSocket route aliasing, 1 MiB payload protection,
assistant grounding enhancements, and deterministic event lifecycle persistence have been
implemented and verified end-to-end against all contract, unit, and black-box acceptance suites.

## Branches & Workspaces

- `main` — mirrors the bootstrap baseline commit. Untouched.
- `integration/v1` — integration trunk. Now at `f25566ac34aef1868ee09ee826d5ef82fc407aec`,
  containing the certified V1 candidate (`fe8f787ab6a1565ad8e1f3b6cbacc5ef6a4bd1ee`)
  merged in full, unsquashed. Not yet merged into `main`.
- `integration/wave2` — Wave 2 integration trunk, branched off `integration/v1`.
  Now at `5c70fc8f308d0589e2e7dae88b3575ded46ddebb`, containing the certified
  Wave 2A/M2A candidate (`bbfd4903af5edb59b453baf9f844de73aad78d09`) merged in
  full, unsquashed. Checked out at `C:\TARS`. Not yet merged into `integration/v1`.
- `feature/v1-backend-voice` — Claude Code backend/voice/memory implementation (SHA: `5f5a1fb`).
- `feature/v1-quality-contracts` — Codex contracts/acceptance harness (SHA: `2c67932`).
- `feature/v1-web-pwa` — Antigravity React/Tauri/PWA foundation (SHA: `5d677db`).
- `feature/v1-integration` — Unified integrated application branch at `C:\TARS-Integration`.
- `feature/wave2-core-skills` — Claude Code Wave 2A skill execution layer (SHA: `6979b8b`, tip; `f900293` implementation).
- `feature/wave2-action-runtime` — Codex Wave 2A action runtime/permission engine (SHA: `a664f78`).
- `feature/wave2-native-shell` — Antigravity Wave 2A HUD/native shell (SHA: `f0dd564`).
- `feature/wave2-m2a-integration` — Merged + integration-fixed Wave 2A candidate at `C:\TARS-Wave2-Integration` (SHA: `bbfd490`).

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
- `apps/backend/actions/` — Wave 2A Action Runtime: permission engine, skill registry, SQLite-backed action store/audit log, confirmation token flow, REST router (`app/routers/actions.py`).
- `apps/backend/skills/` — Wave 2A skills (`windows_app`, `filesystem`, `browser`, `terminal`, `obsidian`) plus `voice_bridge.py`'s deterministic voice-phrase → ActionRequest parser, wired into both the realtime voice pipeline and the REST-based PTT flow.
- `apps/web/` — React 19 + TypeScript + Vite PWA + Tauri 2 desktop shell, real-time WebSocket client with strict contract schema validation, Companion face UI, Active Setups view, Alert history, Memory/Research view with SQLite FTS5 search, Ask TARS assistant chat view, Voice control with PTT, Settings, and (Wave 2A) the HUD overlay with deterministic command bypass, action confirmation/result views, and native active-window/tray/hotkey/autostart bridge (`apps/web/src-tauri/`).
- `contracts/` — Frozen canonical schemas: `trading-event.schema.json` (v1.0.0), `assistant-message.schema.json` (v1.0.0), `action-request.schema.json` (v1.0.0), `action-result.schema.json` (v1.0.0).
- `tests/`, `tools/` — Contract test fixtures (6 valid, 7 malformed), codegen tooling, black-box acceptance runner (`tools/run_acceptance.py`), test client (`tools/tars_test_client.py`), Playwright UI viewport tests (desktop & iPhone 390x844).
- `docs/coordination/handoffs/integration.md` — Integration handoff report.

## Next recommended action

`integration/v1` merge is done and pushed; `integration/wave2` merge (Wave
2A/M2A) is done and pushed on top of it. Remaining decisions, both
explicitly deferred: when to merge `integration/v1` into `main`, and when
to merge `integration/wave2` into `integration/v1`. Wave 2B worktrees
(context windows, control runtime, vision/browser) have been created off
the Wave 2A release SHA — see `docs/coordination/wave2/` for the base spec
once Wave 2B implementation begins.
