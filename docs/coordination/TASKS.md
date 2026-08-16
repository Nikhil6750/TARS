# TASKS.md — TARS Task Board

Status values: `PROPOSED` (not yet authorized), `READY` (authorized,
unclaimed), `IN_PROGRESS`, `BLOCKED`, `DONE`. Each agent updates the status
of its own tasks and adds detail to its own handoff file
(`docs/coordination/handoffs/<agent>.md`), not here — this file tracks
*what*, handoffs track *how it went*.

No implementation has been authorized yet. Everything below is `PROPOSED`
first-wave scope for when a coordinator gives the go-ahead, kept here so
each agent can plan without re-deriving scope from `MASTER_SPEC.md` every
session.

## Claude Code — `backend/`

- [ ] PROPOSED: Scaffold FastAPI app (`backend/app/`) with health endpoint.
- [ ] PROPOSED: SQLite models/migrations for conversation history and alert
      history (`backend/storage/`).
- [ ] PROPOSED: WebSocket endpoint broadcasting `trading-event` +
      companion-state messages.
- [ ] PROPOSED: Mock trading-event generator (`backend/events/`) emitting
      schema-valid events per `contracts/trading-event.schema.json`.
- [ ] PROPOSED: Voice provider interfaces (`backend/voice/`):
      `WakeWordProvider`, `SpeechToTextProvider`, `AssistantProvider`,
      `TextToSpeechProvider`, each with a mock/local implementation.
- [ ] PROPOSED: Claude/Anthropic adapter implementing `AssistantProvider`.
- [ ] PROPOSED: OpenAI adapter implementing `SpeechToTextProvider`.
- [ ] PROPOSED: Fish Audio adapter implementing `TextToSpeechProvider`.
- [ ] PROPOSED: openWakeWord (or equivalent) adapter implementing
      `WakeWordProvider`, laptop-only, with push-to-talk fallback path.

## Codex — `tests/`, `scripts/`

- [ ] PROPOSED: Contract validation harness — validate backend event output
      and frontend event consumption against `contracts/*.schema.json`.
- [ ] PROPOSED: Integration test scaffold across backend/frontend WebSocket
      boundary.
- [ ] PROPOSED: CI wiring (once a CI target is chosen by the coordinator).

## Antigravity — `frontend/`

- [ ] PROPOSED: Scaffold React + TypeScript + Vite app, PWA manifest,
      responsive/mobile-first shell.
- [ ] PROPOSED: WebSocket client consuming `trading-event` +
      companion-state updates.
- [ ] PROPOSED: Active setups view (symbol, direction, entry, SL, TP, R:R,
      risk).
- [ ] PROPOSED: Alert history view.
- [ ] PROPOSED: Companion state/face UI (idle/listening/thinking/
      speaking/alert).
- [ ] PROPOSED: Chat/voice interaction surface — text input always; voice
      input via push-to-talk on both laptop and iPhone (no background
      wake-word claim on iPhone).
- [ ] PROPOSED: Verify responsive behavior on laptop and iPhone viewport
      sizes in-browser.

## Cross-cutting

- [ ] PROPOSED: Coordinator decides `main` vs `integration/v1` merge
      strategy once first-wave implementation lands.
- [ ] PROPOSED: Coordinator assigns first-wave tasks (flip relevant items
      above from `PROPOSED` to `READY`) and confirms directory ownership
      still matches `AGENTS.md`.
