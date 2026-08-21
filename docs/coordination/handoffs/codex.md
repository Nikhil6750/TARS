# Handoff — Codex

Owned directories: `tests/`, `tools/` (contract and acceptance verification,
integration/quality harness). Only Codex edits this file. See
[`AGENTS.md`](../../../AGENTS.md) for the full handoff protocol.

---

## Latest handoff — core-experience recovery evidence

**Branch**: `feature/tars-core-experience-v2`

**Commit SHA**: `1b6fe6207b1cefbb5bda2a7408e7f38e98790266`

**Work completed**:

- Reproduced the requested flow from the latest integrated hot-state base and
  built the source-matched Tauri release. The production binary is
  self-contained: no Vite listener was present during native verification.
- Replaced the old screenshot helper (which wrote into an external
  Antigravity artifact directory) with a Windows-native verifier that selects
  the real `tars-companion.exe` window, captures it with `PrintWindow`, clicks
  Chat/Workspace/Memory/Settings through UI Automation, and fails for a
  missing/clipped window or missing navigation controls.
- Added a source/runtime release-blocker detector. It currently reports 13
  findings rather than treating HTTP/process health as proof of a usable app.
- Added a fixed 30-prompt, 10-category Claude Code/Codex benchmark with seven
  deterministic quality/hygiene checks and a separate bounded Markdown-free
  speech representation. Human correctness review remains explicit.
- Ran all 30 exact prompts through both current CLI adapters. Claude Code
  failed all 30 nested invocations; Codex returned 30 responses but passed
  zero corpus-specific completeness checks, usually asking what repository
  work to perform. No provider winner is claimed.
- Added an offline-only Kokoro A/B/C generator using installed model assets
  and the four required listening lines. Candidates are `am_michael`,
  `am_onyx`, and `bm_george`, with optional current `af_heart` reference.
- Added regression tests for native-window selection, all principal detected
  source blockers, corpus shape/categories, speech sanitization, and internal
  implementation-detail leakage.

**Files changed**:

- `tools/capture_native_tars.py`
- `tools/core_experience_checks.py`
- `tools/assistant_quality_benchmark.py`
- `tools/quality_corpus.json`
- `tools/generate_voice_comparison.py`
- `tools/README.md`
- `tests/unit/test_core_experience_tools.py`
- This Codex handoff file only under shared coordination docs.

**Interfaces exposed**:

- `python tools/core_experience_checks.py --runtime`
- `python tools/capture_native_tars.py --pid <pid> --verify-navigation --output-dir <dir>`
- `python tools/assistant_quality_benchmark.py --provider claude_code --provider codex --output <file>`
- `python tools/generate_voice_comparison.py --include-current-reference --output-dir <dir>`

**Tests run**:

- `python -m pytest tests -q` — 66 passed, 24 expected external/hardware
  skips after `npm ci --prefix tools/codegen`.
- New regression module — 6 passed.
- Ruff on all changed Python files — passed.
- Repository-wide `python -m ruff check tests tools` — nonzero only for 34
  pre-existing violations in unchanged `tools/verify_integrated_voice_runtime.py`.
- Backend targeted voice/assistant/readiness suite — 29 passed.
- Frontend Vitest — 140 passed; TypeScript check and production build passed.
- Frontend lint — nonzero because the configured
  `react-hooks/exhaustive-deps` rule has no installed plugin (two errors).
- Tauri Rust tests — 6 passed, one hardware WGC capture test ignored.
- Source-matched `npm run tauri build` — passed and produced the release EXE
  and NSIS installer in the configured shared Cargo target.
- Native UI Automation run — actual Tauri Chat, Workspace, Memory, and
  Settings controls were invoked and produced four distinct capture hashes;
  actual native captures were saved under untracked `scratch/` evidence.
- Full provider corpus — 30 Claude Code failures; 30 Codex responses,
  174/210 deterministic checks but 0/30 completeness anchors; human review
  still required.

**Known limitations / current release blockers**:

- Launcher requires a copied per-worktree `.env`, can launch/accept a stale
  shared Cargo binary, can confuse an existing port owner with the process it
  started, does not verify the native main webview, and prints `TARS READY` /
  `Say: Hey TARS` while runtime readiness is false.
- Default `compactMode: true` shrinks the full workstation to roughly 380x180;
  navigation works only after manually expanding/summoning the real window.
- Native wake code has only Wake/Command modes, lacks the requested timing
  markers, drops the command tail in a general one-utterance “Hey TARS ...”
  request, and lacks sufficient adaptive-noise/output-suppression diagnostics.
- `/health` and `/runtime/readiness` disagree about the wake provider, while
  Voice Control displays hard-coded provider names unrelated to runtime state.
- Raw streaming Markdown and persisted display Markdown are both passed to
  TTS. The actual native Chat capture also shows raw `**` markers and leaked
  provider/tool wording.
- There is no response-quality contract or task-based Claude/Codex routing in
  the application. The benchmark shows the current CLI invocation context is
  not a valid basis for choosing a provider.
- Physical microphone wake reliability and A/B/C voice quality cannot be
  certified headlessly. A user must perform the required 20/20 wake trial and
  listen to all candidates before selection.

**Exact dependencies required from other agents**:

- Antigravity/frontend + Tauri: implement the explicit wake lifecycle and
  timing telemetry; preserve same-utterance command tails; improve adaptive
  audio handling/output suppression; default to a usable workstation window;
  expose runtime-backed provider state; repair streaming Markdown rendering;
  and separate display text from sanitized spoken text at every TTS entry.
- Claude Code/backend: implement the response-quality contract/composer,
  improve user-facing internal-detail sanitization, add task-based provider
  routing with explicit diagnostics, and make readiness accurately reflect
  the active native wake path without weakening deterministic trading facts.
- Coordinator/integration owner: harden `scripts/start_tars.ps1` around
  worktree env discovery, source/binary provenance, backend PID/port
  ownership, runtime readiness, and native-window verification.
- User/hardware: listen to the generated Kokoro candidates and run the 20/20
  physical “Hey TARS” acceptance matrix after product-owner fixes land.

**Next recommended action**: Product owners implement the enumerated blockers
on their own branches, then run these tools plus the full certification suite.
Do not merge this branch as part of the handoff.

## Latest handoff — overnight agent and safety runtime

**Branch**: `feature/overnight-agent-runtime`

**Commit SHA**: `3582ff1805597cbb0e47a06d2f492264c9ff5050`

**Work completed**:

- Added a durable agent lifecycle runtime with explicit `ON_DEMAND`,
  `SCHEDULED`, and `CONTINUOUS` modes, bounded iterations, lifetime cycle
  limits, provider retries, provider/action/run timeouts, cooperative
  cancellation, due-job scheduling, duplicate ID/dedupe-key protection, and
  explicit interrupted-job recovery.
- Added strict provider-neutral intelligence, orchestrator-decision,
  skill-discovery, and memory-context contracts. Intelligence output is data
  only; it has no execution, risk, confirmation, or verification authority.
- Integrated every proposed skill call through the existing authoritative
  `ActionRuntime`, including pause/resume around one-time confirmation.
- Added `StrategyProvider`, `StrategyDefinition`, typed strategy signals, and a
  read-only `QuantBrainBoundary` that emits no signals when the provider is
  `NOT_CONFIGURED` or failed.
- Added SQLite lifecycle state and append-only redacted audit records, startup
  interruption detection, an explicit recovery transition, scheduler polling
  for due bounded slices, and REST lifecycle endpoints.
- Added adversarial coverage for direct-execution isolation, risk downgrade,
  fabricated `VERIFIED`, unbounded continuous loops, missing strategy
  providers, secret retention, malformed skill calls, provider failures,
  duplicate jobs, timeout, cancellation races, ActionRuntime cancellation,
  scheduling, and recovery.

**Files changed**:

- `apps/backend/agents/` — contracts, provider/skill discovery registries,
  quant boundary, safety, durable store, and lifecycle runtime.
- `apps/backend/app/routers/agents.py`, `app/main.py`, `app/deps.py` — REST,
  lifespan, dependency, and scheduler integration.
- `apps/backend/tests/test_agent_runtime.py` — targeted adversarial suite.
- This handoff and `docs/coordination/overnight/codex.done.json` in the
  completion-metadata commit.

**Interfaces exposed**:

- `POST /api/v1/agents`
- `GET /api/v1/agents/{job_id}`
- `GET /api/v1/agents/{job_id}/audit`
- `POST /api/v1/agents/{job_id}/run`
- `POST /api/v1/agents/{job_id}/cancel`
- `POST /api/v1/agents/{job_id}/recover`
- Python: `AgentRuntime`, `AgentStore`, `AgentDefinition`, `AgentJob`,
  `RuntimeLimits`, `IntelligenceProvider`, `OrchestratorDecision`,
  `SkillDiscoveryProvider`, `MemoryContext`, `StrategyProvider`,
  `StrategyDefinition`, and `QuantBrainBoundary`.

**Tests run**:

- `python -m pytest tests/test_agent_runtime.py tests/test_action_runtime.py
  tests/test_plan_runtime.py -q` — 64 passed.
- Ruff on all changed backend/runtime/test code — passed.
- MyPy on all 12 changed backend source files (`--follow-imports=skip`) —
  passed with no issues.

**Known limitations**:

- No concrete intelligence or `quant_brain` strategy provider is enabled by
  default; the runtime fails closed until an adapter is explicitly registered.
- Continuous agents intentionally execute only scheduler-driven bounded slices;
  there is no detached forever-loop.
- Confirmation is completed through the existing Action API, then the agent is
  explicitly resumed; the runtime never self-confirms.

**Exact dependencies required from other agents**:

- Intelligence-provider owners may register adapters implementing
  `IntelligenceProvider`; adapters return typed decisions only.
- Future `quant_brain` integration must implement read-only `StrategyProvider`
  and preserve source/evidence identifiers.
- UI owners may consume the REST lifecycle and existing ActionRuntime
  confirmation endpoints; no backend dependency is required for correctness.

**Next recommended action**: Integrate one intelligence adapter and the real
read-only `quant_brain` adapter, then exercise an end-to-end scheduled slice
through user confirmation without weakening runtime authority boundaries.

## Previous handoff — Wave 2B control runtime

**Branch**: `feature/wave2b-control-runtime`

**Commit SHA**: `9d17e0fda5fa9db1b2f7a37dc03a5767b664ec80`

**Work completed**:

- Extended the authoritative Wave 2A Action Runtime with persistent,
  objective `ActionPlan`/`ActionStep` models and a synchronous multi-step
  plan state machine.
- Added bounded steps, retries, re-observations, alternate targets, total
  timeout, execution timeout, cancellation, dependency validation, duplicate
  and replay rejection, and no detached/background plan executor.
- Kept permission inheritance per action: every attempt is a fresh canonical
  `ActionRequest`, runtime classification overrides proposed risk, blocked
  operations stop, and confirmation-required steps pause with the exact
  operation and a one-time token.
- Added structured observations for Windows UI Automation, browser, and native
  vision sources plus deterministic `VERIFIED`/`FAILED`/`UNKNOWN`
  verification; missing evidence remains `UNKNOWN` and never succeeds.
- Added password/credential-dialog/secure-desktop/system-critical detection,
  state-change blocking in sensitive contexts, executable-code exclusion from
  plans, credential-field rejection, and recursive audit redaction.
- Added assistant-to-plan conversion that accepts data only and cannot supply
  runtime identity, timestamps, status, provenance, or authorization.
- Added SQLite plan state, observations, and append-only lifecycle audit plus
  REST endpoints for submit, assistant proposal, get, audit, confirm,
  observation, and cancellation.

**Files changed**:

- `apps/backend/actions/plan_models.py`, `plan_requests.py`,
  `plan_runtime.py`, `plan_store.py`, `safety.py`.
- `apps/backend/actions/runtime.py`, `store.py`, `__init__.py`.
- `apps/backend/app/routers/action_plans.py`, `app/main.py`, `app/deps.py`.
- `apps/backend/tests/test_plan_runtime.py`.
- This handoff and `docs/coordination/wave2/m2b/codex.done.json`.

**Interfaces exposed**:

- `POST /api/v1/action-plans`
- `POST /api/v1/action-plans/assistant`
- `GET /api/v1/action-plans/{plan_id}`
- `GET /api/v1/action-plans/{plan_id}/audit`
- `POST /api/v1/action-plans/{plan_id}/confirm`
- `POST /api/v1/action-plans/{plan_id}/observations`
- `POST /api/v1/action-plans/{plan_id}/cancel`
- Python: `ActionPlan`, `ActionStep`, `StructuredObservation`,
  `ActionPlanFactory`, `PlanRuntime`.

**Tests run**:

- `python -m pytest tests/test_action_contracts.py tests/test_action_runtime.py
  tests/test_plan_runtime.py -q` — 53 passed.
- Ruff on changed action, router, app wiring, and targeted tests — passed.
- MyPy on changed backend action/router/app code — passed with no issues in 16
  source files.

**Known limitations**:

- Observation producers (Windows UI Automation, browser DOM, native
  screen/vision) are intentionally interfaces only in this stream.
- Tauri plan visuals are intentionally absent.
- Verification currently uses deterministic expected-state subset matching;
  it does not perform fuzzy/model-authored proof.

**Exact dependencies required from other agents**:

- Windows/browser/vision owners: submit strict `StructuredObservation` data
  bound to the active plan, step, and request IDs; never submit a claimed
  verification status.
- Assistant owners: propose only the documented plan data envelope and never
  call Windows/browser control directly.
- UI owner: render `pending_operation` exactly and return its one-time token to
  the plan confirmation endpoint.

**Next recommended action**: Integrate trusted observation producers and the
plan confirmation/verification UI, then run a real end-to-end control flow
without weakening backend permission or verification authority.

## Previous handoff — Wave 2A action runtime

**Branch**: `feature/wave2-action-runtime`

**Commit SHA**: `6f9e13770edc10ac64c83fe6f25880033e74b8f4`

**Work completed**:

- Built the Wave 2A action runtime as the only skill-execution path, with
  request-age validation, duplicate-ID rejection, deterministic permission
  classification, dispatch, result normalization, and failure conversion.
- Added an independently derived permission floor for all M2A skill families,
  explicit fail-closed policies for unknown capabilities, and terminal-command
  analysis that blocks destructive/elevated/system-critical operations while
  requiring confirmation for other state-changing commands.
- Implemented one-time, expiring confirmation capabilities. Only token hashes
  are persisted; user-supplied `confirmed`/risk fields have no authorization
  effect; blocked actions can never reach confirmation or execution.
- Added durable SQLite request/result state plus append-only audit transitions,
  including requests, validation denials, policy blocks, invalid confirmation
  attempts, confirmation decisions, execution starts, successes, and failures.
- Added strict active-window context attachment (process/title/bounds only), a
  safe assistant proposal adapter that accepts only skill/action/arguments, and
  fixed-phrase deterministic action routing that does not call an LLM.
- Added REST endpoints for intake, result lookup, audit, capabilities,
  confirmation, assistant proposals, and deterministic resolution, plus a
  dedicated `/ws/actions` result stream.
- Added adversarial tests for bypass attempts, duplicates, malformed actions,
  stale/future replay, confirmation replay/expiry, blocked commands, invalid
  tokens, dishonest/malformed skill results, execution failure, context
  smuggling, audit history, and WebSocket delivery.

**Files changed**:

- `apps/backend/actions/` — runtime, permission engine, skill registry, durable
  store, request adapters, and errors.
- `apps/backend/app/routers/actions.py` — Wave 2A Action API and WebSocket.
- `apps/backend/app/main.py`, `apps/backend/app/deps.py` — runtime lifecycle and
  dependency wiring.
- `apps/backend/tests/test_action_runtime.py` — targeted action security and
  lifecycle suite.
- This Codex handoff and `docs/coordination/wave2/m2a/codex.done.json` in the
  follow-up completion-metadata commit.

**Interfaces exposed**:

- `POST /api/v1/actions`
- `GET /api/v1/actions/{request_id}`
- `POST /api/v1/actions/{request_id}/confirm`
- `GET /api/v1/actions/{request_id}/audit`
- `GET /api/v1/actions/audit`
- `GET /api/v1/actions/capabilities`
- `POST /api/v1/actions/assistant`
- `POST /api/v1/actions/resolve`
- `WS /ws/actions`
- Python: `ActionRuntime`, `SkillRegistry`, `PermissionEngine`,
  `ActionRequestFactory`, and `DeterministicActionRouter`.
- Skill loading: `skills.SKILLS` mapping/iterable or `skills.get_skills()`.

**Tests run**:

- `python -m pytest tests/test_action_contracts.py tests/test_action_runtime.py -q`
  — 33 passed.
- `python -m ruff check actions app/routers/actions.py app/main.py app/deps.py
  tests/test_action_contracts.py tests/test_action_runtime.py` — passed.
- `python -m mypy actions app/routers/actions.py app/main.py app/deps.py` — passed
  with no issues in 10 source files.
- Full V1 certification was intentionally not rerun, per Wave 2A validation
  cadence and the user's instruction.

**Known limitations**:

- Concrete Windows skill execution is intentionally absent from this branch;
  the runtime currently starts with an empty registry until Claude's
  `apps/backend/skills/` stream is integrated.
- Native HUD/tray/global-hotkey confirmation rendering is intentionally absent;
  Antigravity owns that stream.
- The custom wake-word path remains `UNVERIFIED`; this stream makes no claim
  that it works end-to-end.

**Exact dependencies required from other agents**:

- Claude Code: expose concrete skill instances through `skills.SKILLS` or
  `skills.get_skills()` and feed recognized global PTT/wake-word actions through
  `ActionRuntime.submit()` / the Action API, never direct skill execution.
- Antigravity: consume the Action API and `/ws/actions`, render exact requested
  arguments for confirmation, and return the one-time confirmation token to the
  confirmation endpoint.
- Integration coordinator: merge all three Wave 2A streams without squashing
  and run the single full M2A integration validation gate.

**Next recommended action**: Integrate the concrete Windows skill registry and
native HUD streams, then run M2A end-to-end validation across request → confirm
→ skill → audit/result.

## Previous handoff — final codegen remediation

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
