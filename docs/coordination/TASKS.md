# TASKS.md — TARS Task Board

Status values: `PROPOSED` (not yet authorized), `READY` (authorized,
unclaimed), `IN_PROGRESS`, `BLOCKED`, `DONE`. Each agent updates the status
of its own tasks and adds detail to its own handoff file
(`docs/coordination/handoffs/<agent>.md`), not here — this file tracks
*what*, handoffs track *how it went*.

Per the Git workflow rule in `AGENTS.md`: each task below becomes its own
`feature/*` branch off `integration/v1` when it moves to `IN_PROGRESS` — do
not batch unrelated tasks into one branch, and do not work on these
directly on `integration/v1` or `main`.

No implementation has been authorized yet. Everything below is `PROPOSED`
first-wave scope, updated for the architecture amendment in
`ARCHITECTURE.md` / `DECISIONS.md` (ADR-010 onward), kept here so each agent
can plan without re-deriving scope every session.

## Claude Code — `apps/backend/`

- [ ] PROPOSED: Scaffold FastAPI app (`apps/backend/app/`) with Pydantic
      models and a health endpoint.
- [ ] PROPOSED: SQLite models/migrations for conversation history, alert
      history, and session state (`apps/backend/storage/`).
- [ ] PROPOSED: WebSocket endpoint broadcasting `trading-event` +
      companion-state messages.
- [ ] PROPOSED: Mock trading-event generator (`apps/backend/events/`)
      emitting schema-valid events per `contracts/trading-event.schema.json`.
- [ ] PROPOSED: Voice provider interfaces (`apps/backend/voice/`):
      `WakeWordProvider`, `SpeechToTextProvider`, `AssistantProvider`,
      `TextToSpeechProvider`, each with a mock/local implementation, wired
      through a Pipecat pipeline.
- [ ] PROPOSED: openWakeWord adapter implementing `WakeWordProvider`
      (phrase "TARS", laptop-only), with push-to-talk/keyboard fallback
      guaranteed regardless of wake-word state.
- [ ] PROPOSED: Silero VAD integration in the Pipecat pipeline.
- [ ] PROPOSED: faster-whisper adapter implementing `SpeechToTextProvider`
      (local, default/required).
- [ ] PROPOSED: Fish Speech adapter implementing `TextToSpeechProvider`
      (local, primary), Kokoro adapter (local, lightweight fallback).
- [ ] PROPOSED: `AssistantProvider` routing (`apps/backend/assistant/`):
      `ClaudeCodeProvider`, `OllamaProvider`, optional
      `AnthropicAPIProvider`, plus the deterministic-vs-model routing logic
      described in `ARCHITECTURE.md` § Assistant architecture.
- [ ] PROPOSED: Memory layer (`apps/backend/memory/`) — SQLite FTS5 search,
      Obsidian vault read/index path, boundary enforcement between
      operational state / conversation memory / research knowledge (per
      `ARCHITECTURE.md` § Memory architecture). `sqlite-vec` only if FTS5
      proves insufficient.
- [ ] PROPOSED: APScheduler wiring for housekeeping jobs (morning/EOD
      summaries, journal tasks, maintenance) — not for live setup
      detection.
- [ ] PROPOSED: OpenTelemetry instrumentation (request/tool-call/latency/
      error logging, no secrets).

## Codex — `tests/`, `tools/`

- [ ] PROPOSED: Contract validation harness — validate backend event output
      and frontend event consumption against `contracts/*.schema.json`.
- [ ] PROPOSED: Integration test scaffold across the backend/frontend
      WebSocket boundary.
- [ ] PROPOSED: Acceptance verification for the free/local core path — the
      app must run and pass its core checks with no Anthropic key, no
      OpenAI key, no Fish Audio hosted key, no commercial wake-word service,
      and no cloud database configured.
- [ ] PROPOSED: CI wiring (once a CI target is chosen by the coordinator).

## Antigravity — `apps/web/`

- [ ] PROPOSED: Scaffold single React + TypeScript + Vite app, PWA manifest,
      responsive/mobile-first shell.
- [ ] PROPOSED: Tauri 2 desktop shell (`apps/web/src-tauri/`) around the
      same app — no separate desktop UI implementation.
- [ ] PROPOSED: WebSocket client consuming `trading-event` +
      companion-state updates.
- [ ] PROPOSED: Active setups view (symbol, direction, entry, SL, TP, R:R,
      risk).
- [ ] PROPOSED: Alert history view.
- [ ] PROPOSED: Companion state/face UI (idle/listening/thinking/
      speaking/alert).
- [ ] PROPOSED: Chat/voice interaction surface — text input always; voice
      input via push-to-talk on both laptop and iPhone (no background
      wake-word claim on iPhone, ever).
- [ ] PROPOSED: Desktop notifications via Tauri/native layer; iPhone
      notifications via PWA Web Push where supported.
- [ ] PROPOSED: Verify responsive behavior on laptop and iPhone viewport
      sizes in-browser, plus the Tauri desktop build.

## Cross-cutting

- [ ] PROPOSED: Coordinator decides `main` vs `integration/v1` merge
      strategy once first-wave implementation lands.
- [ ] PROPOSED: Coordinator assigns first-wave tasks (flip relevant items
      above from `PROPOSED` to `READY`, each on its own `feature/*` branch)
      and confirms directory ownership still matches `AGENTS.md`.
- [ ] PROPOSED: Tailscale Serve setup for private laptop→iPhone access
      (documentation/config, not application code).
