# TASKS.md — TARS Task Board

Status values: `PROPOSED` (not yet authorized), `READY` (authorized,
unclaimed / ready for implementation), `IN_PROGRESS`, `BLOCKED`, `DONE`. Each agent updates the status
of its own tasks and adds detail to its own handoff file
(`docs/coordination/handoffs/<agent>.md`), not here — this file tracks
*what*, handoffs track *how it went*.

Per the Git workflow rule in `AGENTS.md`: each agent implements features
exclusively on its dedicated feature branch in its isolated worktree.
Do not work directly on `integration/v1` or `main`.

Wave 1 implementation and integration are **DONE**.

## Claude Code — `apps/backend/` (Branch: `feature/v1-backend-voice`)

- [x] DONE: Scaffold FastAPI app (`apps/backend/app/`) with Pydantic models and health endpoint.
- [x] DONE: SQLite models/migrations for conversation history, alert history, and session state (`apps/backend/storage/`).
- [x] DONE: WebSocket endpoint broadcasting `trading-event` + companion-state messages.
- [x] DONE: Mock trading-event generator (`apps/backend/events/`) emitting schema-valid events per `contracts/trading-event.schema.json`.
- [x] DONE: Voice provider interfaces (`apps/backend/voice/`): `WakeWordProvider`, `SpeechToTextProvider`, `AssistantProvider`, `TextToSpeechProvider`, each with a mock/local implementation, wired through a Pipecat pipeline.
- [x] DONE: openWakeWord adapter implementing `WakeWordProvider` (phrase "TARS", laptop-only), with push-to-talk/keyboard fallback guaranteed regardless of wake-word state.
- [x] DONE: Silero VAD integration in the Pipecat pipeline.
- [x] DONE: faster-whisper adapter implementing `SpeechToTextProvider` (local, default/required).
- [x] DONE: Fish Speech adapter implementing `TextToSpeechProvider` (local, primary), Kokoro adapter (local, lightweight fallback).
- [x] DONE: `AssistantProvider` routing (`apps/backend/assistant/`): `ClaudeCodeProvider`, `OllamaProvider`, optional `AnthropicAPIProvider`, plus deterministic-vs-model routing logic per `ARCHITECTURE.md`.
- [x] DONE: Memory layer (`apps/backend/memory/`) — SQLite FTS5 search, Obsidian vault read/index path, boundary enforcement between operational state / conversation memory / research knowledge.
- [x] DONE: APScheduler wiring for housekeeping jobs (morning/EOD summaries, journal tasks, maintenance).
- [x] DONE: OpenTelemetry instrumentation (request/tool-call/latency/error logging, no secrets).

## Codex — `tests/`, `tools/` (Branch: `feature/v1-quality-contracts`)

- [x] DONE: Contract validation harness — validate backend event output and frontend event consumption against `contracts/*.schema.json`.
- [x] DONE: Integration test scaffold across the backend/frontend WebSocket boundary.
- [x] DONE: Acceptance verification for the free/local core path — the app runs and passes all checks with zero paid keys.
- [x] DONE: Code generation tooling and verification scripts.
- [x] DONE: Black-box acceptance test suite and Playwright multi-viewport UI verification.
- [ ] PROPOSED: CI wiring (once a CI target is chosen by the coordinator).

## Antigravity — `apps/web/` (Branch: `feature/v1-web-pwa`)

- [x] DONE: Scaffold single React + TypeScript + Vite app, PWA manifest, responsive/mobile-first shell.
- [x] DONE: Tauri 2 desktop shell (`apps/web/src-tauri/`) around the same app — no separate desktop UI implementation.
- [x] DONE: WebSocket client consuming `trading-event` + companion-state updates with strict contract validation.
- [x] DONE: Active setups view (symbol, direction, entry, SL, TP, R:R, risk).
- [x] DONE: Alert history view.
- [x] DONE: Companion state/face UI (idle/listening/thinking/speaking/alert).
- [x] DONE: Chat/voice interaction surface — text input always; voice input via push-to-talk on both laptop and iPhone.
- [x] DONE: Desktop notifications via Tauri/native layer; iPhone notifications via PWA Web Push where supported.
- [x] DONE: Verify responsive behavior on laptop and iPhone viewport sizes in-browser, plus Tauri desktop configuration.

## Integration & Verification (Branch: `feature/v1-integration`)

- [x] DONE: Merge `feature/v1-backend-voice`, `feature/v1-quality-contracts`, `feature/v1-web-pwa` into `feature/v1-integration`.
- [x] DONE: Reconcile route aliases (`/health`, `/api/events`, `/api/assistant/messages`, `/api/voice/status`, `/ws`).
- [x] DONE: Implement 1 MiB payload limit middleware returning HTTP 413.
- [x] DONE: Wire frontend real API and WebSocket endpoints (`active_snapshot`, `/api/v1/assistant/query`, `/api/v1/memory/search`).
- [x] DONE: Run full 15/15 black-box acceptance test suite with zero failures.
- [x] DONE: Produce final integration handoff report.
