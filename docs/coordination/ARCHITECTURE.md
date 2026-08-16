# ARCHITECTURE.md — TARS Technical Architecture

Status: architecture amendment (post-bootstrap, pre-implementation). Product
scope lives in [MASTER_SPEC.md](MASTER_SPEC.md); the rationale behind this
stack is logged in [DECISIONS.md](DECISIONS.md) (ADR-010 onward). Read only
the section relevant to your task — do not read this whole file into
context if you only own one layer.

**Core principle**: the required TARS runtime works fully local/free. Paid
providers (`AnthropicAPIProvider`, hosted Fish Audio TTS, hosted STT, a
cloud database, etc.) are optional adapters behind the provider interfaces
below — never a requirement to run TARS.

## Stack

- **Backend**: Python 3.12, FastAPI, Pydantic models, WebSocket for
  deterministic trading events.
- **Desktop**: React + TypeScript + Vite, wrapped by **Tauri 2**.
- **iPhone**: the *same* React application, shipped as an installable,
  responsive PWA. There is exactly one UI codebase — Tauri and the PWA are
  two shells around it, not two apps.
- **Realtime transport**: WebSocket (backend → clients, live trading events
  + companion state).
- **Storage**: SQLite — lightweight TARS state/history only (conversation
  log, alert history, session state). Never strategy research, backtests,
  or validation data — that is `quant_brain`'s database, not TARS's. See
  § Memory architecture for the full boundary model.
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
├── apps/
│   ├── backend/                      # owned by Claude Code
│   │   ├── app/                      # FastAPI app, routers, WS server
│   │   ├── voice/                    # provider interfaces + Pipecat pipeline
│   │   ├── assistant/                # AssistantProvider routing + adapters
│   │   ├── events/                   # mock trading-event generator
│   │   ├── memory/                   # SQLite + FTS5/sqlite-vec + vault access
│   │   └── storage/                  # SQLite models/migrations
│   └── web/                          # owned by Antigravity
│       ├── src/                      # shared React+TS source
│       ├── public/
│       └── src-tauri/                # Tauri 2 desktop shell
└── tests/                            # owned by Codex
    └── tools/                        # contract + acceptance verification, harness scripts
```

Directories not yet present will be created by the owning agent when
implementation begins — not during this amendment stage.

## Data flow (target end-state)

```
Market data
    ↓
quant_brain
    ↓
canonical API/events  (contracts/trading-event.schema.json)
    ↓
TARS backend (FastAPI + WebSocket + SQLite, apps/backend/)
    ↓
┌────────────┬─────────────────────┬─────────────┐
Laptop        iPhone                 ESP32-S3
(Tauri 2      (same React app,       (later, MQTT +
 desktop      installable PWA,       LVGL, same
 shell)       via Tailscale Serve)   event contract)
└────────────┴─────────────────────┴─────────────┘
```

### V1 substitution

In V1, `quant_brain` is replaced by a mock trading-event generator living in
`apps/backend/events/`. It must emit only events that validate against
`contracts/trading-event.schema.json` — unchanged and unaffected by this
stack amendment (see ADR-004; do not modify the frozen schema for
implementation-stack reasons). This is a strict boundary: the mock generator
is a drop-in for whatever `quant_brain` eventually becomes — the backend,
frontend, and clients must never be coded against mock-generator-specific
assumptions that a real `quant_brain` feed wouldn't satisfy.

## Backend responsibilities (Claude Code ownership — `apps/backend/`)

- FastAPI HTTP API (Pydantic request/response models) for: conversation/
  chat, alert history query, health/status.
- WebSocket endpoint broadcasting `trading-event` and companion-state
  updates to connected clients (laptop, iPhone, later ESP32).
- SQLite persistence for: conversation history, alert/event history, session
  state, plus FTS5 full-text search and optional local semantic retrieval
  (see § Memory architecture). Schema owned entirely within
  `apps/backend/storage/` and `apps/backend/memory/`.
- Mock trading-event generator (`apps/backend/events/`) as the V1 stand-in
  for `quant_brain`, emitting schema-valid events on a timer/simulated
  basis.
- Voice orchestration pipeline built on **Pipecat**
  (`apps/backend/voice/`), with concrete providers behind the interfaces
  below. Every interface ships a mock/local implementation with zero
  external dependencies, so the backend runs with no API keys configured.
- Assistant routing (`apps/backend/assistant/`) implementing
  `AssistantProvider` — see § Assistant architecture.
- Lightweight local scheduler (APScheduler) for periodic, non-live-event
  work — see § Scheduling.
- OpenTelemetry instrumentation — see § Observability.

## Frontend responsibilities (Antigravity ownership — `apps/web/`)

- One React + TypeScript + Vite codebase, responsive/mobile-first,
  PWA-ready (installable on iPhone home screen) and wrapped by **Tauri 2**
  for the Windows desktop build. No second, parallel UI implementation for
  desktop vs. iPhone.
- WebSocket client consuming `trading-event` + companion-state updates.
- Views: active setups, alert history, companion state/face, chat/voice
  interaction surface.
- Voice UX on iPhone: push-to-talk and/or in-app foregrounded listening
  only. Must not claim or imply background wake-word listening while the
  PWA is backgrounded or closed — iOS does not allow it.
- Desktop voice UX integrates with the backend's Pipecat/openWakeWord
  pipeline via the same WebSocket/API surface — no browser-side wake-word
  modeling requirement.
- Desktop notifications via the Tauri/native notification layer; iPhone
  notifications via PWA Web Push where supported (see § Notifications).
- Browser/E2E verification of the above.

## Quality/integration responsibilities (Codex ownership — `tests/`, `tools/`)

- Contract and acceptance verification: every producer (backend mock
  generator, future quant_brain adapter) and consumer (frontend, tests)
  must conform to `contracts/*.schema.json`.
- Integration harness across the backend/frontend boundary (WebSocket
  contract tests, API contract tests).
- CI wiring as it becomes relevant — not part of this amendment stage.

## Voice orchestration

Real-time conversational voice is orchestrated by **Pipecat**, over a
WebRTC / Pipecat-supported self-hosted transport (no dependency on a
commercial real-time voice platform). Retain the four provider interfaces
so no concrete implementation is hard-coded into business logic:

- **`WakeWordProvider`** — **openWakeWord**, local, laptop-only in V1,
  listening for the primary wake phrase **"TARS"**. Push-to-talk and
  keyboard activation are **guaranteed fallbacks**, always available
  regardless of wake-word reliability. The iPhone PWA does **not** claim to
  continuously listen for the wake word while backgrounded or closed — that
  is not something iOS permits; iPhone voice input is push-to-talk and/or
  in-app foregrounded listening only.
- **VAD**: **Silero VAD**, local.
- **`SpeechToTextProvider`** — **faster-whisper**, running locally, as the
  default/required implementation. A hosted STT adapter (e.g. OpenAI
  transcription) may exist later as an optional, non-default provider.
- **`TextToSpeechProvider`** — primary candidate **Fish Speech** running
  locally; a lightweight alternative/fallback implementation using
  **Kokoro** for lower-resource situations. Neither implementation is
  hard-coded into business logic — both sit behind `TextToSpeechProvider`.
  Fish Audio's *hosted* API may be supported later as an additional,
  optional provider, but must never be necessary for TARS to function.

## Assistant architecture

Retain `AssistantProvider` as the single interface the rest of the backend
talks to. Implemented/planned concrete providers:

- **`ClaudeCodeProvider`** — a high-intelligence provider available for the
  user's personal TARS installation, where their existing authenticated
  Claude Code environment permits it. This is distinct from calling the
  Anthropic API directly: it rides on the user's own Claude Code session.
- **`OllamaProvider`** — a local/offline runtime for open-weight models.
  **Claude does not run inside Ollama** — `OllamaProvider` and
  `ClaudeCodeProvider` are separate, independent adapters; never conflate
  the two or imply one implies the other.
- **`AnthropicAPIProvider`** (optional) — direct Anthropic API access, for
  installs that choose to configure a paid API key. Never required.

TARS does not force every command through an LLM. Routing is conceptually:

```
deterministic TARS command/state request
    → deterministic code (no model call)

simple language transformation / lightweight reasoning
    → local model (OllamaProvider), when appropriate

complex research/reasoning
    → the stronger configured AssistantProvider
      (ClaudeCodeProvider or AnthropicAPIProvider)
```

**Trading facts must always come from deterministic TARS/quant_brain
state, never from model invention** — regardless of which
`AssistantProvider` answers a question, it is given deterministic state as
context; it does not generate trading facts itself.

## Memory architecture

Four kinds of memory, with explicit boundaries — TARS must never blur them:

| Layer | Storage | Contains |
|---|---|---|
| **Operational state** | SQLite | Live/derived TARS state: session state, alert/event history, companion state. Source of truth for "what is happening now." |
| **Conversation memory** | SQLite | Chat/voice turn history (`assistant-message` records). Context for the assistant, not evidence of anything. |
| **Research knowledge** | Obsidian-compatible Markdown vault | Human-authored/curated notes, human-readable and human-editable outside TARS. |
| **Trading journal / strategy-experiment records** | Owned by `quant_brain` (referenced, not duplicated) | Validated trading intelligence, backtests, experiment results. TARS reads/displays via the future quant_brain adapter; it never originates or fabricates this data. |

Search:

- **SQLite FTS5** for full-text search over conversation memory and the
  Obsidian vault's indexed text.
- **Local semantic retrieval**, using `sqlite-vec` or an equivalent
  lightweight local extension, added only if FTS5 relevance proves
  insufficient in practice — not introduced speculatively.
- Embedding models used for semantic retrieval must be **local and
  provider-neutral** (no mandatory cloud embedding API).
- Do **not** introduce a standalone vector database unless measurements
  later justify one.

**TARS must never use memory text (conversation memory, journal notes) as
proof of trading performance.** Performance claims are only ever backed by
`quant_brain`'s validated records.

## Connectivity

**Tailscale Serve** is the preferred private method for reaching the
laptop-hosted TARS instance from the user's iPhone — no public exposure
required. **Tailscale Funnel is not used for normal TARS operation** (it
exposes the service publicly). LAN-based development access remains
supported alongside Tailscale.

## Notifications

- **Windows**: Tauri/native notification layer.
- **iPhone**: PWA Web Push, where supported by iOS/Safari.

Notifications are **outputs** of the event-driven trading-event stream —
never the mechanism that decides trading logic. The strategy/validation
engine is `quant_brain`; notifications only surface what it (or the V1 mock
generator) has already decided.

## Scheduling

A lightweight local scheduler (**APScheduler**, or an equivalently stable
lightweight alternative) handles periodic, non-live work only: morning
summaries, end-of-day summaries, research housekeeping, journal tasks,
maintenance. **Scheduled polling is not used as the primary mechanism for
live trade setups** when an event-driven source (the WebSocket trading-event
stream) already exists — polling is for housekeeping, not for setup
detection.

## Observability

Instrument the backend with **OpenTelemetry**. Langfuse integration is
optional (not required for V1). No large self-hosted observability stack is
required for V1 — local/lightweight export is sufficient.

TARS must be able to log:

- assistant requests
- retrieved context identifiers
- tool calls
- tool results metadata
- latency
- errors

**Never log secrets** (API keys, tokens, credentials) in any of the above.

## quant_brain boundary

Reaffirms ADR-001: `quant_brain` remains the single source of truth for
backtesting, transaction costs, walk-forward validation, DSR/statistical
validation, strategy experiments, and validated trading intelligence. TARS
must not reimplement any of these. Future integration is:

- a typed, deterministic API/events surface (extending the same contract
  discipline as `contracts/trading-event.schema.json`), plus
- MCP tools for controlled assistant access to `quant_brain` data —
  read/query access for the assistant layer, not a path for TARS to
  generate or alter validated trading intelligence itself.

## ESP32 future architecture (not implemented this stage)

Planned physical client, for a later stage:

- **Hardware**: ESP32-S3 with an integrated display. No touchscreen is
  required. Physical buttons/an encoder may be added later. No breadboard
  is required for the core device when using an ESP32-S3 board with
  integrated display — a breadboard is only an optional development tool
  for prototyping external buttons, LEDs, microphones, buzzers, or similar
  peripherals.
- **Transport**: MQTT, via Mosquitto or a compatible broker, carrying
  trading/device state.
- **GUI**: LVGL.
- **Contract**: consumes the same `contracts/trading-event.schema.json`
  events as the laptop/iPhone clients — no ESP32-specific event shape.

ESP32 firmware is explicitly out of scope for this stage.

## Trading event contract

Canonical schema: [`contracts/trading-event.schema.json`](../../contracts/trading-event.schema.json).
**Unchanged by this architecture amendment** — the schema is frozen per
ADR-004 and a change of implementation stack is not, by itself, grounds to
modify it. See [MASTER_SPEC.md](MASTER_SPEC.md#trading-event-contract-summary)
for the product-level rationale and [DECISIONS.md](DECISIONS.md) for the
versioning rule.

## Assistant/chat message contract

Canonical schema: [`contracts/assistant-message.schema.json`](../../contracts/assistant-message.schema.json).
**Unchanged by this amendment.** Its `providers.stt` / `providers.assistant`
/ `providers.tts` fields are free-form strings (no enum), so new provider
names introduced by this amendment — `openwakeword`, `faster_whisper`,
`fish_speech`, `kokoro`, `claude_code`, `ollama`, `anthropic_api` — are
already representable without a schema change.
