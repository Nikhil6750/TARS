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

---

## Architecture amendment (2026-08-16) — free/local-first production stack

A deeper tool audit, performed after the bootstrap was approved but before
parallel implementation began, changed several implementation choices
below. This amendment was committed on branch
`feature/architecture-free-stack`, branched from `integration/v1` at
`bd7b745927a35b7b2ae11d2facae3ac6a685e1e7`. It does not reverse any
non-negotiable boundary from ADR-001, ADR-003, ADR-004, ADR-006, or ADR-007
— those stand unchanged. It supersedes implementation-level specifics in
ADR-002, ADR-005, and ADR-008, as noted per-ADR below.

## ADR-010: Desktop ships as Tauri 2 wrapping one shared React codebase; supersedes part of ADR-002

**Date**: 2026-08-16
**Decision**: Desktop is React + TypeScript + Vite wrapped by **Tauri 2**.
iPhone is the *same* React application, shipped as an installable/responsive
PWA. There is exactly one frontend codebase — never two independent UI
implementations for desktop vs. iPhone. Backend stack (Python 3.12,
FastAPI, WebSocket, SQLite) is unchanged from ADR-002; Pydantic models are
now explicit.
**Why**: A plain browser-only frontend under-serves the laptop experience
(no native notifications, no offline packaging); building a second native
desktop UI would violate "one source of truth" and double frontend
maintenance. Tauri gives a native shell around the same React app the
iPhone already needs.
**Status**: Accepted. Supersedes ADR-002's "React + TypeScript frontend...
responsive/mobile-first, PWA-ready" insofar as it now explicitly includes a
Tauri 2 desktop shell around that same codebase; the backend portion of
ADR-002 is unchanged.

## ADR-011: Voice stack is Pipecat + openWakeWord + Silero VAD + faster-whisper + Fish Speech/Kokoro; supersedes ADR-005's adapter list

**Date**: 2026-08-16
**Decision**: Real-time conversational voice is orchestrated by **Pipecat**
over WebRTC / a Pipecat-supported self-hosted transport. Wake word is
**openWakeWord** (phrase: "TARS"), VAD is **Silero VAD**, STT is
**faster-whisper** running locally, TTS's primary candidate is **Fish
Speech** running locally with **Kokoro** as a lightweight
alternative/fallback. Push-to-talk and keyboard activation remain
guaranteed fallbacks regardless of wake-word reliability. Hosted adapters
(OpenAI STT, Fish Audio hosted TTS) may exist later as optional, non-default
providers. The `WakeWordProvider`, `SpeechToTextProvider`,
`TextToSpeechProvider` interfaces from ADR-005 are retained unchanged; only
the concrete default adapters change.
**Why**: The prior default adapters (OpenAI STT, Fish Audio hosted TTS)
required paid API keys for the core voice loop, contradicting the
requirement that the core runtime work free/local. Pipecat gives a
maintained orchestration layer instead of hand-rolling one.
**Status**: Accepted. Supersedes ADR-005's specific adapter choices for STT
and TTS (OpenAI, Fish Audio hosted) as the *default* path — those become
optional adapters. ADR-005's interface list and "every interface must ship a
mock/local implementation" requirement are unchanged and reaffirmed.

## ADR-012: Assistant providers are ClaudeCodeProvider, OllamaProvider, optional AnthropicAPIProvider; supersedes ADR-005's assistant adapter

**Date**: 2026-08-16
**Decision**: `AssistantProvider` implementations are `ClaudeCodeProvider`
(rides the user's own authenticated Claude Code environment — a
high-intelligence option for personal installs, not an Anthropic API call),
`OllamaProvider` (local/offline runtime for open-weight models — Claude does
**not** run inside Ollama, the two are independent adapters), and an
optional `AnthropicAPIProvider` for installs that choose to configure a
paid key. Routing is conceptual, not literal LLM-for-everything: deterministic
command/state requests resolve in deterministic code; simple language
transformation may use the local model; complex research/reasoning goes to
the stronger configured provider. Trading facts always come from
deterministic TARS/quant_brain state, never model invention.
**Why**: ADR-005 named a single "Claude/Anthropic adapter" without
distinguishing a free path (Claude Code, Ollama) from a paid one (direct
Anthropic API), and without stating that not every command needs a model
call at all. This ADR makes the free/local path the default and the paid
path explicitly optional.
**Status**: Accepted. Supersedes ADR-005's "Claude/Anthropic adapter
(initial)" language for `AssistantProvider`; the `AssistantProvider`
interface itself is unchanged.

## ADR-013: Memory architecture — SQLite + Obsidian vault + FTS5, with explicit boundaries

**Date**: 2026-08-16
**Decision**: Operational truth lives in SQLite. Human-readable knowledge
lives in an Obsidian-compatible Markdown vault. Search is SQLite FTS5, plus
optional local semantic retrieval via `sqlite-vec` or an equivalent
lightweight local extension, added only if justified by measurement — no
standalone vector database introduced speculatively. Embedding models used
for semantic retrieval must be local and provider-neutral. Explicit
boundaries are maintained between: operational state, research knowledge,
conversation memory, trading journal, and strategy/experiment records (the
latter two remain `quant_brain`'s domain, referenced not duplicated). Memory
text is never used as proof of trading performance.
**Why**: Without an explicit boundary model, conversation memory or
free-text notes could get treated as if they were validated trading
history, which would violate ADR-001's quant_brain boundary. Naming the
boundary now, before implementation, prevents that drift.
**Status**: Accepted. New decision, not a supersession — ADR-002's "SQLite
for lightweight TARS state/history only" is the operational-state layer of
this fuller model.

## ADR-014: Connectivity via Tailscale Serve, not Funnel

**Date**: 2026-08-16
**Decision**: Tailscale Serve is the preferred private method for reaching
the laptop-hosted TARS instance from the user's iPhone. Tailscale Funnel
(public exposure) is not used for normal TARS operation. LAN-based
development access remains supported.
**Why**: TARS holds trading-related information and should not be reachable
from the public internet by default; Tailscale Serve gives private
device-to-device access without requiring any public exposure or a hosted
relay TARS would need to trust.
**Status**: Accepted.

## ADR-015: Notifications are outputs only — Tauri native + PWA Web Push

**Date**: 2026-08-16
**Decision**: Windows notifications go through the Tauri/native notification
layer; iPhone notifications go through PWA Web Push where supported.
Notifications are strictly outputs of the event-driven trading-event
stream — never a mechanism that decides trading logic.
**Why**: Keeps the notification layer thin and prevents it from becoming a
second, informal decision path outside `quant_brain`/the event contract.
**Status**: Accepted.

## ADR-016: APScheduler for housekeeping only; event-driven stays primary for live setups

**Date**: 2026-08-16
**Decision**: A lightweight local scheduler (APScheduler or an equivalently
stable alternative) handles periodic non-live work: morning/end-of-day
summaries, research housekeeping, journal tasks, maintenance. Scheduled
polling is not used as the primary mechanism for live trade setups when an
event-driven source (the WebSocket trading-event stream) already exists.
**Why**: Prevents polling from silently becoming the real setup-detection
mechanism by accretion — the event-driven contract is the source of truth
for live setups.
**Status**: Accepted.

## ADR-017: OpenTelemetry required (lightweight), Langfuse optional, no secrets logged

**Date**: 2026-08-16
**Decision**: Backend is instrumented with OpenTelemetry. Langfuse
integration is optional, not required for V1. No large self-hosted
observability infrastructure is required for V1. TARS must be able to log
assistant requests, retrieved context identifiers, tool calls, tool result
metadata, latency, and errors — and must never log secrets.
**Why**: Debuggability of the assistant/voice pipeline matters from day
one, but standing up a heavy observability stack would contradict the
free/local-first, low-infrastructure goal of V1.
**Status**: Accepted.

## ADR-018: ESP32 future architecture — ESP32-S3, MQTT, LVGL; no firmware this stage

**Date**: 2026-08-16
**Decision**: The planned physical client is an ESP32-S3 board with
integrated display (no touchscreen required; physical buttons/encoder may
be added later; no breadboard required for the core device — breadboards
are optional prototyping tools for external peripherals only). Transport is
MQTT via Mosquitto or a compatible broker. GUI is LVGL. It consumes the same
`contracts/trading-event.schema.json` events as other clients. Firmware is
not implemented in this or the prior stage.
**Why**: Names the target hardware/transport/GUI stack now so the event
contract and backend are never designed in a way that assumes a browser,
without pulling ESP32 work forward into active scope.
**Status**: Accepted, deferred implementation.

## ADR-019: Repository layout renamed to apps/backend/ and apps/web/; Codex ownership includes tests/ and tools/; supersedes ADR-008's directory names

**Date**: 2026-08-16
**Decision**: Source directories are renamed: `backend/` → `apps/backend/`
(Claude Code), `frontend/` → `apps/web/` (Antigravity, containing the
shared React app plus its Tauri 2 shell). Codex's owned directories are
`tests/` and `tools/`. The ownership *principle* from ADR-008 — one agent
per directory, no two agents share a directory, contracts/docs read-only
during parallel work — is unchanged.
**Why**: `apps/` groups the two deployable applications (backend, web)
under one parent now that web includes both a PWA and a Tauri desktop
shell; `tools/` gives Codex a home for harness scripts distinct from
`tests/` itself.
**Status**: Accepted. Supersedes only the directory *names* in ADR-008; the
ownership principle and read-only rule for `contracts/`/`docs/coordination/`
are reaffirmed unchanged.

## ADR-020: Permanent Git workflow rules

**Date**: 2026-08-16
**Decision**: From this point forward: never implement features directly on
`main`; never implement features directly on `integration/v1` except
explicit integration work; every feature/phase/meaningful sub-feature/
architectural change gets its own feature branch off `integration/v1`; each
completed logical unit gets one meaningful commit; push after the unit
passes its relevant validation; no empty/noise commits; do not squash
development history when merging `integration/v1` into `main` unless the
user explicitly requests it; never modify git `user.name`/`user.email`;
never fabricate authorship; every handoff entry must contain branch +
commit SHA.
**Why**: Once three agents work in parallel, undisciplined branching or
direct commits to shared branches produce merge conflicts and unclear
history; these rules were made explicit and permanent before parallel work
starts, not after the first conflict.
**Status**: Accepted, permanent. Recorded in full in `AGENTS.md` § Git
workflow, which is the operational copy agents should follow day-to-day —
this ADR is the decision record for *why*.

## ADR-021: V1 candidate certified and merged into `integration/v1`

**Date**: 2026-08-17
**Decision**: `fe8f787ab6a1565ad8e1f3b6cbacc5ef6a4bd1ee` (tip of
`feature/v1-final-candidate-2`) is the independently certified V1 release
candidate. It was merged into `integration/v1` via an explicit `--no-ff`
merge commit `f25566ac34aef1868ee09ee826d5ef82fc407aec`, preserving full,
unsquashed commit history (no rebase, no squash, no rewrite). Only a
lightweight post-merge sanity check was run (merge-conflict-marker scan,
JSON contract schema parse, `py_compile` on key backend entrypoints) — full
certification was intentionally not re-run since the exact candidate SHA
was already independently certified prior to this merge. `integration/v1`
was pushed to `origin`. `main` was deliberately **not** merged in this
action.
**Why**: Certification already happened against this exact SHA; re-running
the full suite here would be redundant. Recording the merge as its own ADR
gives a durable, append-only pointer from the certified SHA to where it
landed in `integration/v1`, independent of any branch getting deleted or
force-pushed later.
**Status**: Accepted. Merging `integration/v1` into `main` remains a
separate, later coordinator decision — not authorized by this entry.

## ADR-022: Wave 2A (M2A) candidate certified and merged into `integration/wave2`

**Date**: 2026-08-17
**Decision**: `bbfd4903af5edb59b453baf9f844de73aad78d09` (tip of
`feature/wave2-m2a-integration`) is the certified Wave 2A milestone
candidate — Claude's core skills (`f900293`), Codex's action runtime
(`a664f780fa7f93e12032e4e3f90ce04db791f2d7`), and Antigravity's native
shell (`f0dd56425e1918d80b93f32e4375d46c31abad5e`) merged with full
history preserved, plus integration fixes: skill registry/MemoryService
DI wiring, permission allowlist gaps (`windows_app.list_running`,
`browser.search`), HUD↔backend contract alignment (argument field names,
confirmation token flow, removal of a frontend fallback that fabricated
`ActionResult`s when the native shell's backend was unreachable), a
filesystem search time bound, and deterministic voice-phrase routing into
the real Action Runtime (including two bugs found only by testing against
genuine speech-to-text output — see `apps/backend/skills/voice_bridge.py`
and `apps/web/src/services/actions.ts`). It was merged into
`integration/wave2` via an explicit `--no-ff` merge commit
`5c70fc8f308d0589e2e7dae88b3575ded46ddebb`, preserving full, unsquashed
commit history. Only a lightweight post-merge sanity check was run
(merge-conflict-marker scan, JSON contract schema parse, `py_compile` on
key backend entrypoints, `create_app()`/skill-registry construction
smoke test) — the full milestone suite was intentionally not re-run since
this exact candidate SHA was already independently verified prior to this
merge (238/239 backend tests passing, one pre-existing environment-only
failure; 59/59 frontend tests; ruff/mypy/typecheck/lint clean; a real
Tauri release build and native runtime exercise). `integration/wave2` was
pushed to `origin`. `integration/v1` and `main` were deliberately **not**
touched by this action.
**Known non-blocking verification gaps** (see the Wave 2A integration
owner's final report for full detail): tray-icon click, autostart-toggle
UI, and native notification are mechanism-reviewed but not literally
click-exercised; the full live-microphone→speech→app-capture PTT loop is
unverified (STT itself was verified against real synthesized speech via a
real faster-whisper model; only the browser microphone-capture link
inside the Tauri webview was not exercised); wake word remains
unverified, unchanged from Wave 1/V1. None of these block the milestone —
each is either a pre-existing gap outside this integration's scope or a
UI-automation limitation of the verification environment, not a defect in
the merged code.
**Why**: Certification already happened against this exact SHA; re-running
the full suite here would be redundant. Recording the merge as its own ADR
gives a durable, append-only pointer from the certified SHA to where it
landed in `integration/wave2`, and records the known gaps in one place so
Wave 2B doesn't need to rediscover them.
**Status**: Accepted. Merging `integration/wave2` into `integration/v1` or
`main` remains a separate, later coordinator decision — not authorized by
this entry.
