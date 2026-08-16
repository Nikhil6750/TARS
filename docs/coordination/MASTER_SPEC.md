# MASTER_SPEC.md — TARS Product Specification

Status: bootstrap (v1 planning). This is the source-of-truth product spec.
Architectural/technical detail lives in
[ARCHITECTURE.md](ARCHITECTURE.md); decisions log lives in
[DECISIONS.md](DECISIONS.md).

## What TARS is

TARS is an AI-powered quantitative trading **companion** — an
always-available interface between the user and:

- trading setup scanners
- `quant_brain` (future)
- risk information
- alerts
- strategy research
- Claude
- an ESP32 physical companion (later)

## What TARS is not

- **Not** an autonomous trading bot. It never places trades, never will in
  this codebase's scope.
- **Not** a backtesting/validation engine. That is `quant_brain`'s job,
  permanently.
- **Not** a source of fabricated confidence — no fake AI confidence
  percentages, no invented strategy performance.

## Platforms (V1)

1. Windows laptop (primary development target)
2. iPhone, via a responsive/PWA web interface (no native app in V1)
3. ESP32 physical companion — **later**, consuming the same API/event
   contracts as the laptop/iPhone clients. Nothing in the contract or API
   design should assume a browser; ESP32 must be able to consume the same
   WebSocket events.

## V1 product scope

The first useful version of TARS must eventually provide:

- Laptop interface and iPhone-responsive interface, both live against the
  same backend.
- Real-time connection (WebSocket) between backend and clients.
- Companion state/face — a visible indicator of TARS's current state (idle,
  listening, thinking, speaking, alert).
- Mock trade alerts carrying: symbol, direction, entry, stop loss, take
  profit, risk:reward, risk percent.
- Setup lifecycle visibility: developing → confirmed/valid → invalidated.
- Alert history.
- Voice input and spoken response.
- A Claude-backed conversation surface for questions such as:
  - "TARS, show active setups."
  - "TARS, why did gold not trigger?"
  - "TARS, what requires my attention?"

### Explicitly deferred (not V1)

- `quant_brain` integration — V1 uses mock trading events generated locally.
- ESP32 physical companion.
- Any trading execution capability (permanently out of scope, not just
  deferred).

## Architectural principles (non-negotiable)

TARS must remain a **thin companion/interface**. It must never duplicate
`quant_brain`'s:

- backtesting
- cost modelling
- walk-forward validation
- DSR / statistical validation
- strategy research database

Target end-state data flow:

```
Market data
    ↓
quant_brain
    ↓
canonical API/events
    ↓
TARS backend
    ↓
┌────────────┬─────────────┐
Laptop UI    iPhone UI     ESP32
└────────────┴─────────────┘
```

For V1, the "quant_brain" box is replaced by a mock trading-event generator
that emits events conforming to
[`contracts/trading-event.schema.json`](../../contracts/trading-event.schema.json),
so swapping in the real `quant_brain` later requires no contract or backend
redesign — only a new event source.

## Recommended V1 architecture (summary)

See [ARCHITECTURE.md](ARCHITECTURE.md) for full detail; this stack was
amended after bootstrap (see [DECISIONS.md](DECISIONS.md) ADR-010 onward) to
make the required core path entirely free/local.

- Python 3.12, FastAPI backend (Pydantic models, WebSocket for live events)
- One React + TypeScript + Vite frontend, wrapped by Tauri 2 for desktop and
  shipped as an installable/responsive PWA for iPhone — not two separate UI
  codebases
- WebSocket for live events
- SQLite only, for lightweight TARS state/history (not for strategy
  research data — that belongs to `quant_brain`)
- No trading execution anywhere in the stack

## Voice

Voice architecture must be provider-neutral, defined behind interfaces:

- `WakeWordProvider`
- `SpeechToTextProvider`
- `AssistantProvider`
- `TextToSpeechProvider`

Full implementation detail (Pipecat orchestration, openWakeWord, Silero VAD,
faster-whisper, Fish Speech/Kokoro) lives in
[ARCHITECTURE.md](ARCHITECTURE.md#voice-orchestration). Product-level
constraints, unchanged by the stack amendment:

- **Wake word**: "TARS". Push-to-talk and keyboard activation are
  guaranteed fallbacks regardless of wake-word reliability — never block V1
  on a perfect wake-word model.
- **iPhone**: do NOT claim background wake-word listening works — iOS
  restricts this. V1 iPhone support is push-to-talk and/or in-app listening
  while the web app is foregrounded, plus spoken responses.
- Every external/paid voice service must have a mock/local fallback so the
  application runs fully without any API keys — the default STT/TTS/wake-word
  path is local, not merely mock.

## Trading event contract (summary)

Full schema: [`contracts/trading-event.schema.json`](../../contracts/trading-event.schema.json).

Minimum fields: `schema_version`, `event_id`, `timestamp`, `source`,
`symbol`, `strategy_id`, `state`, `direction`, `entry`, `stop_loss`,
`take_profit`, `risk_reward`, `risk_percent`, `validation_status`,
`reason_codes`, `warnings`, `expires_at`.

Valid `state` values include concepts such as: `IDLE`,
`SETUP_DEVELOPING`, `SETUP_VALID`, `SETUP_INVALIDATED`, `RISK_WARNING`,
`SYSTEM_WARNING`.

No fake AI confidence percentages anywhere in this contract. This schema is
depended on by the backend, frontend, iPhone client, ESP32 (later), and the
future `quant_brain` adapter — treat changes to it as breaking-change events
requiring a `schema_version` bump and coordinator sign-off.

## Multi-agent execution model

See [AGENTS.md](../../AGENTS.md) for the full ownership/handoff protocol.
Summary: Claude Code owns backend + assistant/voice; Codex owns contract
verification + integration/quality harness; Antigravity owns frontend +
responsive iPhone experience + browser verification. No two agents own the
same source directory; shared architecture files are read-only during
parallel implementation unless a coordinator explicitly authorizes a change.
