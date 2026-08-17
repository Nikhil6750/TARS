# Handoff — Codex

Owned directories: `tests/`, `tools/` (contract and acceptance verification,
integration/quality harness). Only Codex edits this file. See
[`AGENTS.md`](../../../AGENTS.md) for the full handoff protocol.

---

## Latest handoff

**Branch**: `fix/v1-final-codegen`

**Commit SHA**: `b071208ba9710f307f9399c38b1d6a3c2c86ba0e` (last implementation commit
before this handoff)

**Work completed**:

- Fixed the final Windows contract-codegen blocker without changing canonical
  schemas or backend/frontend product logic.
- Identified the root cause as `core.autocrlf=true` producing CRLF working-tree
  artifacts while fresh subprocess output used a mixture of host-dependent and
  explicit LF endings, followed by a raw byte comparison.
- Canonicalized every generated Python and TypeScript artifact to LF after the
  pinned subprocess generators finish.
- Made drift comparison normalize newline encodings only. Semantic content,
  whitespace other than line endings, generator drift, and file-set drift remain
  fatal.
- Added narrowly scoped Git attributes pinning only generated Python and
  TypeScript contract artifacts to LF checkout behavior.
- Added Windows/LF regressions using real codegen, including repeated byte-level
  generation, CRLF-equivalent comparison, non-newline content drift, and a
  temporary semantic schema type change.

**Files changed**:

- `.gitattributes`
- `tools/generate_contracts.py`
- `tools/README.md`
- `tests/contracts/test_codegen_newlines.py`
- This Codex handoff file only under shared coordination docs.
- Generated artifacts were regenerated exclusively through the generator and
  have no tracked content diff. `contracts/` is unchanged from the exact base.

**Interfaces exposed**:

- `generate(destination)` now always writes canonical LF artifact bytes.
- `check_drift(generated)` accepts only newline-equivalent LF/CRLF content while
  retaining strict file-set and semantic content checks.
- Generated checkout policy is `text eol=lf` only for
  `tools/generated/python/*.py` and `tools/generated/typescript/*.d.ts`.

**Tests run**:

- `python tools/generate_contracts.py` twice — byte-identical SHA-256 sets; all
  five artifacts LF-only.
- `python tools/generate_contracts.py --check` — passed.
- `python -m pytest tests/contracts -q` — 33 passed.
- `python -m pytest tests -q` — 56 passed, 24 expected external-acceptance skips.
- `python -m ruff check tests tools` — passed.
- `git diff --exit-code -- tools/generated` after second generation — clean.
- `git diff --exit-code 9f13e09a7c44be27901d34cb885a63ceb8755463 -- contracts`
  — clean; canonical schemas unchanged.

**Known limitations**:

- None for final contract/codegen remediation. The 24 skipped tests are the
  existing external acceptance cases that intentionally require the
  process-owning harness, not codegen failures.

**Exact dependencies required from other agents**:

- None.

**Next recommended action**: Coordinator may independently verify from a clean
clone and review `fix/v1-final-codegen`; do not merge as part of this handoff.

## Previous handoff — certification coverage

**Branch**: `fix/v1-certification-coverage`

**Commit SHA**: `3ec3c845a519c91cc4d27ea1e64234d7703129a8` (last implementation commit
before this handoff)

**Work completed**:

- Expanded the suite from 55 to 77 collected tests and from 15 to 24 external
  acceptance cases.
- Added immutable lifecycle-history verification for DEVELOPING → VALID →
  INVALIDATED, including distinct IDs, retained valid history, separate
  invalidation history, and active-state removal.
- Added a browser test that imports the actual TypeScript WebSocket client and
  runs it against the actual backend for active snapshots, all lifecycle states,
  heartbeat pong, and reconnect.
- Added deterministic backend and browser voice-plumbing checks covering audio
  bytes → STT → arbitrary returned transcript → assistant → backend TTS → audio
  playback, with an explicit prohibition on `speechSynthesis` substitution.
- Added Content-Length, HTTP/1.0 no-length, and true chunked streaming payload
  regressions. Failed streaming cases cannot persist and contaminate later tests.
- Added Obsidian/FTS source provenance checks at the public API and actual
  assistant-grounding boundary, plus absent-statistics anti-fabrication checks.
- Added real-mode source and visible-UI scans for fabricated Sharpe, DSR,
  expectancy, win-rate, drawdown, profitability, and strategy-performance claims.
- Added deterministic npm/Cargo/Tauri config/build-metadata validation and
  optional native `cargo check`/Tauri build execution when Rust tooling exists.
- Added `tools/run_certification.py`, which always invokes codegen drift,
  backend pytest/Ruff/MyPy, frontend install/tests/typecheck/lint/build, Tauri,
  and process-owning external acceptance. Nonzero gate results remain fatal.
- Hardened the process-owning runner against stale services and moved default
  certification ports to isolated 8765/5179.

**Files changed**:

- `tests/acceptance/`: lifecycle, WebSocket, voice, payload, memory, metrics,
  viewport, and security coverage.
- `tests/integration/`: actual backend voice pipeline and memory-grounding chain.
- `tests/certification/`: fabricated-metric and Tauri checks.
- `tests/unit/`: client, runner, scanner, and gate-behavior tests.
- `tools/`: external client extensions, scanners, acceptance/certification
  runners, Tauri checks, README, and Ruff-only import cleanup.
- This handoff file only under shared coordination docs. No product source or
  canonical contract was modified.

**Interfaces exposed**:

- `python tools/run_certification.py`
- `python tools/tauri_checks.py --cargo-check`
- `python tools/run_acceptance.py ...` now rejects pre-existing endpoints.
- `TarsTestClient.history_for_symbol(...)`
- `TarsTestClient.search_memory(...)`
- Route override: `TARS_MEMORY_SEARCH_PATH`.

**Tests run**:

- `python -m pytest tests --collect-only -q` — 77 collected.
- `python -m pytest tests/unit -q` — 17 passed.
- `python -m ruff check tests tools` — passed.
- Isolated external acceptance on ports 8767/5181 — 19 passed, 5 failed on
  the intended unmodified product blockers.
- Backend pytest — 63 passed.
- Backend MyPy — passed across 55 source files.
- Frontend Vitest — 12 passed.
- Frontend TypeScript — passed.
- Frontend production build — passed.
- Full certification entry point — correctly nonzero; every mandatory gate ran.

**Known limitations / current certification blockers**:

- Lifecycle invalidation reuses the valid `event_id`, overwriting valid history.
- Backend broadcasts `{type, event}` while the real frontend unwraps
  `{type, payload}`; snapshot works but live lifecycle events do not.
- Frontend PTT does not send recorded bytes to STT, hard-codes
  `Show active setups`, and substitutes browser speech synthesis for backend TTS.
- Chunked payloads bypass the 1 MiB middleware and reach schema handling (422
  rather than 413). Content-Length and no-length safety cases pass.
- Public FTS retrieval preserves `source_id`, but assistant grounding drops it.
- Real-mode Memory UI exposes sample `DSR > 1.8` and `Sharpe 2.12` claims.
- Tauri Rust/npm minor versions mismatch; `Cargo.lock`, `build.rs`, and the
  configured tray icon are missing. Rust/Cargo are not installed locally, so the
  native build was correctly skipped after deterministic checks failed.
- Ruff fails only in unmodified `apps/backend/run.py` (E402). Codex-owned paths
  are Ruff-clean.
- Frontend has no `lint` script, so the mandatory lint gate fails.
- Generated contract artifacts drift in Python `__init__.py` and both generated
  TypeScript declarations.

**Exact dependencies required from other agents**:

- Claude Code/backend: make invalidation append a new event ID; enforce the body
  limit while streaming; retain memory `source_id` in grounding; fix Ruff E402 in
  `apps/backend/run.py`; coordinate the canonical live WebSocket envelope with
  frontend without changing frozen contracts.
- Antigravity/frontend: consume the backend live-event envelope; implement actual
  recorded-audio STT/assistant/backend-TTS playback; remove real-mode sample
  metrics; add and satisfy lint; align Tauri npm/Cargo versions and add required
  Rust/Tauri build metadata/assets.
- Coordinator: decide whether regenerated artifacts should be committed after
  confirming the pinned codegen output; install Rust/MSVC to execute the native
  Tauri build after manifest blockers are fixed.

**Next recommended action**: Product owners fix the enumerated backend/frontend
blockers on their own branches, then rerun `python tools/run_certification.py`.
Do not weaken the five failing external regressions or the mandatory quality
gates.
