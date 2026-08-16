# ARCHITECTURE.md — TARS Technical Architecture

Status: bootstrap (v1 planning), no implementation yet. Product scope lives
in [MASTER_SPEC.md](MASTER_SPEC.md). Read only the section relevant to your
task — do not read this whole file into context if you only own one layer.

## Stack

- **Backend**: Python 3.12, FastAPI
- **Frontend**: React + TypeScript, Vite, responsive/mobile-first, PWA-ready
- **Realtime transport**: WebSocket (backend → clients, live trading
  events + companion state)
- **Storage**: SQLite — lightweight TARS state/history only (conversation
  log, alert history, session state). Never strategy research, backtests,
  or validation data — that is `quant_brain`'s database, not TARS's.
- **No trading execution** anywhere in this stack.

## Repository layout (target — not yet created)

```
TARS/
├── AGENTS.md
├── README.md
├── .env.example
├── .gitignore
├── contracts/                        # shared, read-only during parallel work
│   ├── trading-event.schema.json
│   └── assistant-message.schema.json
├── docs/
│   └── coordination/
│       ├── MASTER_SPEC.md
│       ├── ARCHITECTURE.md
│       ├── CURRENT_STATE.md
│       ├── DECISIONS.md
│       ├── TASKS.md
│       └── handoffs/
│           ├── claude.md
│           ├── codex.md
│           └── antigravity.md
├── backend/                          # owned by Claude Code
│   ├── app/                          # FastAPI app, routers, WS server
│   ├── voice/                        # provider interfaces + adapters
│   ├── events/                       # mock trading-event generator
│   └── storage/                      # SQLite models/migrations
├── frontend/                         # owned by Antigravity
│   ├── src/
│   └── public/
└── tests/                            # owned by Codex
    └── (contract + integration tests)
```

Directories not yet present will be created by the owning agent when
implementation begins — not during this bootstrap stage.

## Data flow (target end-state)

```
Market data
    ↓
quant_brain
    ↓
canonical API/events  (contracts/trading-event.schema.json)
    ↓
TARS backend (FastAPI + WebSocket + SQLite)
    ↓
┌────────────┬─────────────┐
Laptop UI    iPhone UI     ESP32
(React/Vite) (React/Vite,  (later, same
             PWA)          event contract)
└────────────┴─────────────┘
```

### V1 substitution

In V1, `quant_brain` is replaced by a mock trading-event generator living in
`backend/events/`. It must emit only events that validate against
`contracts/trading-event.schema.json`. This is a strict boundary: the mock
generator is a drop-in for whatever `quant_brain` eventually becomes — the
backend, frontend, and clients must never be coded against
mock-generator-specific assumptions that a real `quant_brain` feed wouldn't
satisfy.

## Backend responsibilities (Claude Code ownership)

- FastAPI HTTP API for: conversation/chat, alert history query, health/status.
- WebSocket endpoint broadcasting `trading-event` and companion-state
  updates to connected clients (laptop, iPhone, later ESP32).
- SQLite persistence for: conversation history, alert/event history, session
  state. Schema owned entirely within `backend/storage/`.
- Mock trading-event generator (`backend/events/`) as the V1 stand-in for
  `quant_brain`, emitting schema-valid events on a timer/simulated basis.
- Voice subsystem, all behind provider interfaces (see below) so any
  provider can be swapped without touching callers:
  - `WakeWordProvider`
  - `SpeechToTextProvider`
  - `AssistantProvider`
  - `TextToSpeechProvider`
  - Every interface must ship with a mock/local implementation with zero
    external dependencies, so the backend runs with no API keys configured.
- Claude/Anthropic adapter implementing `AssistantProvider`, used for the
  conversational surface ("TARS, show active setups.", etc.).

## Frontend responsibilities (Antigravity ownership)

- React + TypeScript + Vite app, responsive/mobile-first, PWA-ready
  (installable on iPhone home screen).
- WebSocket client consuming `trading-event` + companion-state updates.
- Views: active setups, alert history, companion state/face, chat/voice
  interaction surface.
- Voice UX on iPhone: push-to-talk and/or in-app foregrounded listening
  only. Must not claim or imply background wake-word listening on iOS.
- Laptop voice UX may integrate with the backend's wake-word pipeline via
  the same WebSocket/API surface — no browser-side wake-word modeling
  requirement for V1.

## Quality/integration responsibilities (Codex ownership)

- Automated validation that every producer (backend mock generator, future
  quant_brain adapter) and consumer (frontend, tests) conforms to
  `contracts/*.schema.json`.
- Integration harness across the backend/frontend boundary (WebSocket
  contract tests, API contract tests).
- CI wiring as it becomes relevant — not part of the bootstrap stage.

## Voice provider interfaces (contract-level description)

These are defined at the architecture level now so all agents design against
the same shape; concrete Python interfaces are implemented by Claude Code
during the backend stage.

- **`WakeWordProvider`**: listens locally for the "TARS" wake word (laptop
  only in V1), emits an activation event to the backend. Local-only, no
  cloud dependency. Fallback: disabled, with push-to-talk/hotkey as the
  activation path.
- **`SpeechToTextProvider`**: audio in → text out. Adapter implementations:
  OpenAI transcription (initial), mock (offline, deterministic).
- **`AssistantProvider`**: text/context in → response out. Adapter
  implementations: Claude/Anthropic (initial), mock (offline, canned/echo
  responses for development without API keys).
- **`TextToSpeechProvider`**: text in → audio out. Adapter implementations:
  Fish Audio (initial), mock (offline, no-op or local TTS).

## Trading event contract

Canonical schema: [`contracts/trading-event.schema.json`](../../contracts/trading-event.schema.json).
See [MASTER_SPEC.md](MASTER_SPEC.md#trading-event-contract-summary) for the
product-level rationale. Treated as a versioned, breaking-change-controlled
artifact — see [DECISIONS.md](DECISIONS.md).

## Assistant/chat message contract

Canonical schema: [`contracts/assistant-message.schema.json`](../../contracts/assistant-message.schema.json).
Covers conversation turns between user and TARS across both text and voice
input modes, and optionally references a `trading-event` by `event_id` when
a message is about a specific alert/setup.
