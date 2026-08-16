# DECISIONS.md — TARS Architectural Decision Log

Append-only log. Do not delete or rewrite prior entries — if a decision is
reversed, add a new entry that supersedes it and note the supersession.
Shared/read-only file: edited only by the coordinator, or by an agent that
has been explicitly authorized to record a new decision.

Format: `## ADR-NNN: Title` / Date / Decision / Why / Status.

---

## ADR-001: TARS is a companion, never an execution system

**Date**: 2026-08-16
**Decision**: TARS will never place trades and will never duplicate
`quant_brain`'s backtesting, cost modelling, walk-forward validation,
DSR/statistical validation, or strategy research database.
**Why**: Keeps TARS a thin, replaceable interface layer. Prevents scope
creep into a second, divergent quant research system.
**Status**: Accepted, permanent — not revisitable by a future stage without
explicit user sign-off.

## ADR-002: V1 stack

**Date**: 2026-08-16
**Decision**: Python 3.12 + FastAPI backend; React + TypeScript + Vite
frontend (responsive/mobile-first, PWA-ready); WebSocket for live event
delivery; SQLite for lightweight TARS state/history only.
**Why**: Matches the target platforms (Windows laptop, iPhone web, later
ESP32) with a single backend and a browser-deliverable frontend; SQLite
avoids standing up infrastructure for what is, in V1, a single-user local
companion.
**Status**: Accepted.

## ADR-003: V1 uses mock trading events, not quant_brain

**Date**: 2026-08-16
**Decision**: A local mock trading-event generator stands in for
`quant_brain` in V1. It must only emit events valid against
`contracts/trading-event.schema.json`.
**Why**: `quant_brain` does not exist yet / is out of scope for this stage.
Building against the contract now means swapping in the real `quant_brain`
later requires no backend or frontend redesign.
**Status**: Accepted.

## ADR-004: Trading event contract is frozen at v1.0.0 pending real usage

**Date**: 2026-08-16
**Decision**: `contracts/trading-event.schema.json` ships with
`schema_version: "1.0.0"` and the fields enumerated in `MASTER_SPEC.md`. No
AI confidence percentage field exists or will be added. States are a fixed
enum: `IDLE`, `SETUP_DEVELOPING`, `SETUP_VALID`, `SETUP_INVALIDATED`,
`RISK_WARNING`, `SYSTEM_WARNING`.
**Why**: Backend, frontend, iPhone, ESP32 (later), and the future
`quant_brain` adapter all depend on this contract. Changing it after
multiple consumers exist is a breaking change; freezing it now and requiring
a version bump for any change keeps consumers in sync.
**Status**: Accepted. Any change requires a `schema_version` bump and an
explicit `DECISIONS.md` entry.

## ADR-005: Voice architecture is provider-neutral with mandatory mocks

**Date**: 2026-08-16
**Decision**: Voice is defined behind four interfaces —
`WakeWordProvider`, `SpeechToTextProvider`, `AssistantProvider`,
`TextToSpeechProvider`. Initial concrete adapters: openWakeWord (or
equivalent local provider) for wake word, OpenAI for STT, Claude/Anthropic
for the assistant, Fish Audio for TTS. Every interface ships a mock/local
implementation so the app runs with zero API keys.
**Why**: Avoids locking the product to one vendor per capability, and keeps
local development/testing possible without live credentials.
**Status**: Accepted.

## ADR-006: Wake word is "TARS"; push-to-talk is the guaranteed fallback

**Date**: 2026-08-16
**Decision**: The target wake word is "TARS". If a reliable custom
wake-word model cannot be produced immediately, push-to-talk/hotkey
activation is retained as the fallback rather than blocking on wake-word
accuracy.
**Why**: Custom wake-word models are nontrivial to get reliable; the product
must be usable regardless of wake-word quality.
**Status**: Accepted.

## ADR-007: iPhone voice does not claim background listening

**Date**: 2026-08-16
**Decision**: The iPhone/web client will not implement or claim
background wake-word listening. V1 iPhone voice is push-to-talk and/or
in-app listening while the web app is foregrounded, with spoken responses.
**Why**: iOS platform restrictions make true background wake-word listening
in a web app infeasible; overclaiming this would be a false product promise.
**Status**: Accepted.

## ADR-008: Directory ownership split across three agents

**Date**: 2026-08-16
**Decision**: Claude Code owns `backend/`; Antigravity owns `frontend/`;
Codex owns `tests/`/`scripts/` (contract verification + integration/quality
harness). `contracts/` and `docs/coordination/*.md` (other than an agent's
own handoff file) are shared and read-only during parallel implementation
unless a coordinator explicitly authorizes a change.
**Why**: Prevents merge conflicts and unclear ownership once work
parallelizes across three agents.
**Status**: Accepted.

## ADR-009: Bootstrap docs/contracts committed to `integration/v1`, not `main`

**Date**: 2026-08-16
**Decision**: This bootstrap stage (coordination docs, contracts, repo
scaffolding) is committed directly to a new `integration/v1` branch, since
`main` had zero commits at the start of this stage. No application code
(backend or frontend) is implemented on `integration/v1` during this stage.
**Why**: Establishes the single source of truth all three agents will branch
from once parallel implementation begins, without prematurely deciding
`main`'s trunk contents.
**Status**: Accepted.
