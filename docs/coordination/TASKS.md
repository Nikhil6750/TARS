# TASKS.md — TARS Task Board

Status values: `PROPOSED` (not yet authorized), `READY` (authorized,
unclaimed / ready for implementation), `IN_PROGRESS`, `BLOCKED`, `DONE`. Each agent updates the status
of its own tasks and adds detail to its own handoff file
(`docs/coordination/handoffs/<agent>.md`), not here — this file tracks
*what*, handoffs track *how it went*.

Per the Git workflow rule in `AGENTS.md`: each agent implements features
exclusively on its dedicated feature branch (`feature/v1-backend-voice`,
`feature/v1-quality-contracts`, `feature/v1-web-pwa`) in its isolated worktree.
Do not work directly on `integration/v1` or `main`.

Wave 1 implementation is **AUTHORIZED**. The tasks below are marked `READY`
for parallel execution.

## Claude Code — `apps/backend/` (Branch: `feature/v1-backend-voice`)

- [ ] READY: Scaffold FastAPI app (`apps/backend/app/`) with Pydantic
      models and a health endpoint.
- [ ] READY: SQLite models/migrations for conversation history, alert
      history, and session state (`apps/backend/storage/`).
- [ ] READY: WebSocket endpoint broadcasting `trading-event` +
      companion-state messages.
- [ ] READY: Mock trading-event generator (`apps/backend/events/`)
      emitting schema-valid events per `contracts/trading-event.schema.json`.
- [ ] READY: Voice provider interfaces (`apps/backend/voice/`):
      `WakeWordProvider`, `SpeechToTextProvider`, `AssistantProvider`,
      `TextToSpeechProvider`, each with a mock/local implementation, wired
      through a Pipecat pipeline.
- [ ] READY: openWakeWord adapter implementing `WakeWordProvider`
      (phrase "TARS", laptop-only), with push-to-talk/keyboard fallback
      guaranteed regardless of wake-word state.
- [ ] READY: Silero VAD integration in the Pipecat pipeline.
- [ ] READY: faster-whisper adapter implementing `SpeechToTextProvider`
      (local, default/required).
- [ ] READY: Fish Speech adapter implementing `TextToSpeechProvider`
      (local, primary), Kokoro adapter (local, lightweight fallback).
- [ ] READY: `AssistantProvider` routing (`apps/backend/assistant/`):
      `ClaudeCodeProvider`, `OllamaProvider`, optional
      `AnthropicAPIProvider`, plus the deterministic-vs-model routing logic
      described in `ARCHITECTURE.md` § Assistant architecture.
- [ ] READY: Memory layer (`apps/backend/memory/`) — SQLite FTS5 search,
      Obsidian vault read/index path, boundary enforcement between
      operational state / conversation memory / research knowledge (per
      `ARCHITECTURE.md` § Memory architecture). `sqlite-vec` only if FTS5
      proves insufficient.
- [ ] READY: APScheduler wiring for housekeeping jobs (morning/EOD
      summaries, journal tasks, maintenance) — not for live setup
      detection.
- [ ] READY: OpenTelemetry instrumentation (request/tool-call/latency/
      error logging, no secrets).

## Codex — `tests/`, `tools/` (Branch: `feature/v1-quality-contracts`)

- [ ] READY: Contract validation harness — validate backend event output
      and frontend event consumption against `contracts/*.schema.json`.
- [ ] READY: Integration test scaffold across the backend/frontend
      WebSocket boundary.
- [ ] READY: Acceptance verification for the free/local core path — the
      app must run and pass its core checks with no Anthropic key, no
      OpenAI key, no Fish Audio hosted key, no commercial wake-word service,
      and no cloud database configured.
- [ ] READY: Code generation tooling and verification scripts.
- [ ] PROPOSED: CI wiring (once a CI target is chosen by the coordinator).

## Antigravity — `apps/web/` (Branch: `feature/v1-web-pwa`)

- [ ] READY: Scaffold single React + TypeScript + Vite app, PWA manifest,
      responsive/mobile-first shell.
- [ ] READY: Tauri 2 desktop shell (`apps/web/src-tauri/`) around the
      same app — no separate desktop UI implementation.
- [ ] READY: WebSocket client consuming `trading-event` +
      companion-state updates.
- [ ] READY: Active setups view (symbol, direction, entry, SL, TP, R:R,
      risk).
- [ ] READY: Alert history view.
- [ ] READY: Companion state/face UI (idle/listening/thinking/
      speaking/alert).
- [ ] READY: Chat/voice interaction surface — text input always; voice
      input via push-to-talk on both laptop and iPhone (no background
      wake-word claim on iPhone, ever).
- [ ] READY: Desktop notifications via Tauri/native layer; iPhone
      notifications via PWA Web Push where supported.
- [ ] READY: Verify responsive behavior on laptop and iPhone viewport
      sizes in-browser, plus the Tauri desktop build.

## Cross-cutting / Post-Wave-1

- [ ] PROPOSED: Coordinator decides `main` vs `integration/v1` merge
      strategy once first-wave implementation lands.
- [ ] PROPOSED: Tailscale Serve setup for private laptop→iPhone access
      (documentation/config, not application code).

