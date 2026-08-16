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

Bootstrap stage. Coordination docs and shared contracts exist; no backend
or frontend application code has been implemented yet. See
[docs/coordination/CURRENT_STATE.md](docs/coordination/CURRENT_STATE.md)
for the up-to-date picture.

## Start here

If you are an agent (Claude Code, Codex, Antigravity, or otherwise) about to
work in this repository, **read [AGENTS.md](AGENTS.md) first** — it defines
the session-start protocol, directory ownership, and handoff rules that keep
parallel work conflict-free.

Humans: the same file plus
[docs/coordination/MASTER_SPEC.md](docs/coordination/MASTER_SPEC.md) (what
TARS is) and
[docs/coordination/ARCHITECTURE.md](docs/coordination/ARCHITECTURE.md) (how
it's built) are the fastest way to get oriented.

## Repository map

- [`AGENTS.md`](AGENTS.md) — agent entry point, ownership, handoff protocol.
- [`docs/coordination/`](docs/coordination/) — spec, architecture, current
  state, decision log, task board, and per-agent handoffs.
- [`contracts/`](contracts/) — canonical JSON Schemas (trading events,
  assistant messages) that backend, frontend, iPhone, ESP32 (later), and the
  `quant_brain` adapter (later) all depend on.
- [`.env.example`](.env.example) — configuration template; copy to `.env`
  (gitignored) and fill in only what you need. Every external provider has a
  mock/local mode, so the app runs with zero API keys.

`backend/`, `frontend/`, and `tests/` do not exist yet — they are created by
their owning agent (see `AGENTS.md`) once implementation is authorized.
