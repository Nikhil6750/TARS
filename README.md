# TARS

TARS is an AI-powered quantitative trading **companion** — an
always-available interface between the user and trading setup scanners,
`quant_brain` (future), risk information, alerts, strategy research, and
Claude, running on a Windows laptop and iPhone (via a responsive web
interface) today, with an ESP32 physical companion planned later.

**TARS is not an autonomous trading bot.** It never places trades and never
duplicates `quant_brain`'s backtesting, cost modelling, walk-forward
validation, DSR/statistical validation, or strategy research database.

## Status

Architecture amendment stage (post-bootstrap, pre-implementation).
Coordination docs, architecture, and shared contracts exist; no backend or
frontend application code has been implemented yet. See
[docs/coordination/CURRENT_STATE.md](docs/coordination/CURRENT_STATE.md)
for the up-to-date picture.

## Stack, in short

Python 3.12 + FastAPI + Pydantic backend, one React + TypeScript + Vite
frontend shipped two ways (Tauri 2 for Windows desktop, installable PWA for
iPhone), WebSocket for live trading events, SQLite for TARS's own state.
Voice runs on Pipecat + openWakeWord + Silero VAD + faster-whisper + Fish
Speech/Kokoro. The assistant layer talks to Claude Code or a local Ollama
model by default, with a direct Anthropic API key as an optional add-on.

**The required core path is entirely free/local** — no Anthropic API key,
OpenAI key, Fish Audio hosted key, commercial wake-word service, or cloud
database is needed to run TARS. Paid providers exist only as optional
adapters behind the provider interfaces (`WakeWordProvider`,
`SpeechToTextProvider`, `AssistantProvider`, `TextToSpeechProvider`). Full
rationale in
[docs/coordination/ARCHITECTURE.md](docs/coordination/ARCHITECTURE.md) and
[docs/coordination/DECISIONS.md](docs/coordination/DECISIONS.md).

## Start here

If you are an agent (Claude Code, Codex, Antigravity, or otherwise) about to
work in this repository, **read [AGENTS.md](AGENTS.md) first** — it defines
the session-start protocol, directory ownership, the permanent Git workflow
rule (one feature branch per unit of work, off `integration/v1`, never
directly on `main`), and handoff rules that keep parallel work
conflict-free.

Humans: the same file plus
[docs/coordination/MASTER_SPEC.md](docs/coordination/MASTER_SPEC.md) (what
TARS is) and
[docs/coordination/ARCHITECTURE.md](docs/coordination/ARCHITECTURE.md) (how
it's built) are the fastest way to get oriented.

## Repository map

- [`AGENTS.md`](AGENTS.md) — agent entry point, ownership, Git workflow,
  handoff protocol.
- [`docs/coordination/`](docs/coordination/) — spec, architecture, current
  state, decision log, task board, and per-agent handoffs.
- [`contracts/`](contracts/) — canonical JSON Schemas (trading events,
  assistant messages) that backend, frontend, iPhone, ESP32 (later), and the
  `quant_brain` adapter (later) all depend on. Frozen; unaffected by the
  stack amendment.
- [`.env.example`](.env.example) — configuration template; copy to `.env`
  (gitignored) and fill in only what you need. Every external provider has a
  mock/local mode, so the app runs with zero API keys.

`apps/backend/`, `apps/web/`, and `tests/`/`tools/` do not exist yet — they
are created by their owning agent (see `AGENTS.md`) once implementation is
authorized.
