# Handoff — Codex

Owned directories: `tests/`, `tools/` (contract and acceptance verification,
integration/quality harness). Only Codex edits this file. See
[`AGENTS.md`](../../../AGENTS.md) for the full handoff protocol.

---

## Latest handoff — Market X-Ray final release-candidate review (2026-10-31)

**Branch**: `feature/market-xray-final-verification`

**Implementation commit SHA**: `93c6c2a47deb98395f8418ec78ebd10e781db458`

**Reviewed integration commit / worktree**:
`7fe5875ecbe682f36cf7ded3245089184e44a2f9` /
`C:\TARS-Market-XRay-RC`

**Work completed**:

- Reviewed and executed the exact integrated candidate. Confirmed the earlier
  no-lookahead, live-feed truthfulness, installer, first-run, and desktop
  hardening commits are already ancestors; no older patch was replayed.
- Added real-release-binary ownership scenarios for recovery after an attached
  compatible backend disappears and after an owned backend process is killed.
- Added a deterministic regression for the confirmed Market WebSocket
  disconnect/build race. It is explicitly xfailed until the backend owner fixes
  the sender, so the defect is visible without making unrelated Codex gates red.
- Kept trading execution disabled. No backend, frontend, contract, or UI source
  was modified.

**Files changed**:

- `tools/verify_backend_ownership.py`
- `tests/integration/test_market_ws_disconnect.py`
- This Codex handoff file only.

**Tests run / evidence**:

- Backend full suite: **1,819 passed, 8 skipped, 0 failed** in 19m42s.
- Root contract/integration: **75 passed, 24 skipped, 1 known-defect xfailed**.
- Targeted market/identity/lifecycle: **119 passed**.
- Frontend: **217 Vitest tests passed**; typecheck and production build passed;
  lint has one existing `prefer-const` error in `src/test/orb-native.test.ts:4`.
- Rust: **33 passed, 1 hardware capture ignored, 0 failed**.
- Black-box Market integration: **15/15 passed**. API+Chromium: **27/27**
  passed on four consecutive correctly bound runs; the earlier timeout did not
  reproduce after fixing the verifier environment's IPv4 Vite binding.
- Native desktop: **19/19 passed**, including per-monitor DPI, proven foreground,
  1920x1080 fullscreen, exact restoration, resize, reopen, and orphan-free quit.
- Consolidated ownership/port/single-instance/recovery matrix: **44/44 passed**.
  One cold `--quit` timeout in an earlier loaded run passed in three later runs
  (including the consolidated matrix), so it is retained as intermittent
  evidence rather than claimed as a confirmed application defect.
- Real NSIS installer: **24/24 passed** in an isolated location on this developer
  host; **558 native modules** had no unresolved non-Windows imports. Artifact:
  208,044,916 bytes, SHA-256
  `0574CA45D1688A8C872537644604DBB4F5569241567FE9630F333CA259B9203D`.

**Known limitations / unresolved defects**:

- **Release blocker:** abrupt Market WebSocket disconnects can finish while
  `_build_frame` is in a worker, after which the sender unconditionally calls
  `send_json`. Repeated real Uvicorn runs emit `RuntimeError: Unexpected ASGI
  message 'websocket.send' ... response already completed` at
  `app/routers/market_xray.py:408`. Chromium page teardown reproduced the same
  server error. The focused regression is xfailed on this exact behavior.
- The explicit target supplied for this review is 25 FX pairs plus XAUUSD. The
  registry has 17 required FX pairs plus XAUUSD. Missing required pairs are
  `USDCHF`, `EURCHF`, `EURNZD`, `GBPCHF`, `AUDNZD`, `NZDJPY`, `NZDCAD`, and
  `CADJPY`. `EURCHF` and `GBPCHF` are not even in `UNAVAILABLE_PAIRS`; the API
  coverage note still describes the earlier ambiguous universe and names three
  non-target pairs (`CADCHF`, `GBPNZD`, `NZDCHF`). No data was manufactured and
  no warning was removed.
- A genuine clean-Windows installation remains **BLOCKED**. The real installer
  test used an isolated install/data root but this is still a development host;
  no suitable Windows Sandbox/clean VM is available.
- Native CUA visual inspection was unavailable because its Windows native pipe
  was absent. The purpose-built Win32 desktop verifier supplied independent
  pixel, HWND, DPI, foreground, fullscreen, restoration, and process evidence.

**Exact dependencies required from other agents**:

- Claude Code/backend: recheck the stop event after `_build_frame` returns and
  handle closed-send RuntimeError without hiding unrelated failures; add the
  missing target symbols only when genuine source/live backing exists; correct
  registry coverage metadata and the `/instruments` note.
- Antigravity/frontend: correct the existing `prefer-const` lint error.
- Release coordinator: provide a clean Windows VM for the final installer test.

**Next recommended action**: **NO-GO** until the Market WebSocket send-after-close
race is fixed and the xfailed regression passes. Resolve or explicitly defer the
eight-pair target gap with truthful coverage metadata, then rerun backend,
Chromium, ownership, desktop, and clean-machine installer verification.

---

## Latest handoff — Market X-Ray independent verification (2026-10-31)

**Branch**: `feature/market-xray-independent-verification`

**Implementation commit SHA**: `807c8f2e24ec19bafac4479f29fd481e25188dc6`

**Base / worktree**: `110d07e770606f8c812032878da09b5c3f655548` /
`C:\TARS-Market-XRay-Codex`

**Work completed**:

- Added a black-box integration verifier for backend identity stability,
  adoption compatibility, port conflicts, replay isolation/determinism,
  recorded provenance, feed status, coverage warnings, and the permanent
  trading-execution guard.
- Hardened the native desktop verifier for per-monitor DPI, exact physical
  monitor rectangles, proven foreground ownership before input injection,
  minimized-window signatures, tri-state PASS/FAIL/NOT VERIFIED reporting,
  owned-versus-attached backend lifecycles, and evidence-preserving shutdown.
- Fixed two confirmed Market X-Ray verifier defects: event dedupe now primes
  both first scans before taking its baseline, and the replay UI assertion
  waits for an actual cursor change instead of assuming a 3.2-second response.
  Both browser and integration verifiers now reset shared replay state even
  after a failed assertion.
- Verified the candidate desktop at Antigravity hardening commit
  `d8d2dcb8b65950f76f4feb7f648d8fc127b82e6b`: exact 1920x1080 fullscreen and
  restoration, single-instance routing, owned backend spawn/recovery,
  identity/ownership changes after forced loss, and cooperative no-orphan
  shutdown all passed. The intermittent spontaneous minimized launch did not
  reproduce; the verifier now detects and diagnoses it without cascading
  unrelated failures.

**Files changed**:

- `tools/verify_market_desktop.py`
- `tools/verify_market_xray.py`
- `tools/verify_market_integration.py`
- `tests/unit/test_market_desktop_verifier.py`
- `tests/unit/test_market_integration_verifier.py`
- This Codex handoff file only.

**Interfaces exposed**:

- `python tools/verify_market_integration.py --api http://127.0.0.1:8000`
- `python tools/verify_market_desktop.py --quit-after`
- Desktop JSON evidence now records `status` as `PASS`, `FAIL`, or
  `NOT VERIFIED`, plus backend identity and ownership classification.

**Tests run / evidence**:

- Backend full suite: **1,757 passed, 8 skipped, 2 timing failures** in 73m19s.
  Both failures passed together in isolation (**2 passed**) and are therefore
  recorded as load-sensitive suite flakiness, not hidden as a clean full run.
- Frontend Vitest: **207 passed**; TypeScript `--noEmit`: passed.
- Root contract/integration suite: **75 passed, 24 expected skips** after
  linking the already-installed pinned codegen dependency.
- New verifier regressions: **9 passed**; Ruff and `git diff --check`: passed.
- Market API + Chromium: **27/27 passed**. Independent black-box API suite:
  **15/15 passed**. Trading execution was `DISABLED` in API and native UI.
- Desktop harness run: 17 checks passed, zero application failures, two F11
  checks correctly reported NOT VERIFIED when Win32 denied foreground. A
  separate native UI invocation then proved fullscreen geometry
  `(0,0)-(1920,1080)` and restoration to `(48,11)-(1871,1017)`.
- Owned backend PID 31960 was forcibly stopped; supervisor recovered PID 33344
  with a new instance/owner identity. Second launch retained one shell and one
  listener. Cooperative quit left zero shell/backend processes and listeners.

**Known limitations / unresolved defects**:

- Clean NSIS installation is **BLOCKED**: this host has no Windows Sandbox,
  Hyper-V service, clean user profile, or separate clean Windows VM. Development
  execution was not substituted. The tested installer SHA-256 is
  `BFD48DB154F1A1164ADDA2FEEBDEAF7184581F9CE5E724AF8AAF78B0EA32C90F`.
- Candidate packaging is not releasable: `tauri.conf.json` declares no bundle
  resources, while `supervisor.rs` expects installed
  `backend-runtime/python/python.exe` and `backend/run.py`. The NSIS file is
  only 4,094,171 bytes and contains no adjacent bundled runtime evidence.
- Aborting/reloading a client on `/api/v1/market/ws` can race the sender after
  close and log an unhandled `RuntimeError: Unexpected ASGI message
  'websocket.send'` at `apps/backend/app/routers/market_xray.py:331`.
- Loss of an attached external backend was not replaced within 90 seconds;
  owned-child loss recovered correctly. The desktop owner must confirm whether
  takeover after external loss is required or intentionally deferred to a new
  shell launch.
- Candidate Rust tests were not completed because Cargo exhausted the host's
  remaining disk while building a fresh detached worktree. This is an
  environment block, not a claimed Rust pass.
- Empty-store behavior and installed desktop/companion coexistence remain part
  of the blocked clean-install gate. Spontaneous minimized launch remains
  unreproduced, so no root cause is claimed.

**Exact dependencies required from other agents**:

- Antigravity/desktop: bundle the backend runtime and application resources in
  NSIS; decide and test attached-backend loss policy; review the no-resource
  installer before producing another release candidate.
- Claude/backend: serialize/cancel the Market WebSocket sender during client
  disconnect so it cannot send after close; investigate the two load-sensitive
  agent-runtime timeouts without weakening their assertions.
- Coordinator/environment: provide a genuinely clean Windows VM/profile with
  no developer dependencies and enough disk for Rust verification.

**Next recommended action**: owners fix the WebSocket close race and NSIS
runtime packaging, produce a new installer, then rerun this branch's black-box,
desktop, Cargo, and clean-install gates. Do not merge this branch into `main` or
`integration/v1` as part of the handoff.

## Latest handoff — minimal golden voice loop awaiting physical certification (2026-08-22)

**Branch**: `feature/tars-minimal-golden-loop`

**Implementation commit SHA**: `2c383377d0f1550bb57c8653a8f5e10a0ce98b11`

**Base SHA / worktree**:
`eeb117a4c8474f7da82c6229b0dcf8f453525225` /
`C:\TARS-worktrees\tars-minimal-golden-loop`. The hot-state tip
`f7a7310c861c2fdddde357cf67b42519a2c927f0` is an ancestor of this base.

**Work completed**:

- Studied `Avinashb722/jarvis-ai-assistant` and `open-jarvis/OpenJarvis`, then
  reduced the production turn to one backend owner without copying either
  implementation.
- Added `AssistantTurnController` with one utterance/turn ID/execution/final
  response/TTS invariant, in-flight joining, completed replay, and conflicting
  turn-ID rejection.
- Made `POST /api/v1/voice/utterance` the only production audio entry. Native
  Rust now owns microphone capture, segmentation, WAV transport, and playback
  state only. React observes backend state and plays backend-produced audio.
- Added configurable normalized transcript wake aliases, one-shot commands,
  and a seven-second two-stage `Hey TARS` / `Yeah?` command window.
- Added the explicit deterministic/normal/chart/tool/research/trading router.
  Normal conversation directly calls one configured primary and uses strict
  sequential fallback only after a provider error.
- Unified the response as `AssistantResponse` with separate Markdown-capable
  `display_text` and backend-sanitized natural `speech_text`; Kokoro synthesis
  chunks only complete sentences.
- Preserved hot chart state, WGC watcher/capture, asynchronous deep
  verification, action/permission, skill, memory, research fail-closed, and
  quant-brain boundaries behind the controller.
- Hardened `scripts/start_tars.ps1` to discover the primary worktree `.env`,
  reject a foreign port owner, wait for real provider readiness, build from the
  current source into a worktree-local target, and verify the finished native
  page-load marker.
- Built and launched the actual app. Runtime probe reports zero findings;
  backend PID `22940` points to this worktree's `run.py`, and native PID `17576`
  points to this worktree's release EXE with a responsive `TARS Ready` window.

**Files changed**:

- Backend: `.env.example`, `apps/backend/app/{config,deps,main,readiness,voice_state,voice_telemetry}.py`,
  `apps/backend/app/routers/{assistant,health,voice}.py`,
  `apps/backend/assistant/{factory,hot_chart_state_store,provider_router,turn_controller}.py`,
  migration `0008_golden_voice_trace.sql`, and the associated backend tests.
- Native/web: `apps/web/src-tauri/{Cargo.lock,Cargo.toml}`, native
  `src/{lib,wake_engine}.rs`, `App.tsx`, assistant/HUD/voice components,
  `runtime/{AssistantClient,VoiceAssistantRuntime,WakeClient}.ts(x)`,
  `services/{audio,native-bridge}.ts`, new response/runtime types, and associated
  frontend tests.
- Removed active duplicate frontend paths:
  `apps/web/src/services/{speech,wake-word}.ts` and tests
  `{single-utterance-wake,speech}.test.ts`.
- Quality/operations: `docs/runtime/GOLDEN_LOOP_INVENTORY.md`,
  `scripts/start_tars.ps1`, golden acceptance/integration tests,
  `tools/{README,core_experience_checks,generate_voice_comparison}.py`, and
  `artifacts/voice-samples/{manifest.json,voice_A.wav,voice_B.wav,voice_C.wav}`.

**Interfaces exposed**:

- `POST /api/v1/voice/utterance` — canonical multipart native utterance entry.
- `POST /api/v1/assistant/query` and its stream — canonical text entry through
  the same controller.
- `AssistantResponse` — `turn_id`, `display_text`, `speech_text`, `intent`,
  `status`, `provider`, `latency_ms`, plus correlation/audio fields.
- `GET /api/v1/diagnostics/voice-latency` — per-turn requested timing markers
  and exact failure stage.
- `scripts\start_tars.ps1` — one-command production launch.

**Tests run**:

- Backend full suite: **670 passed**, one third-party deprecation warning.
- Root contract/integration suite: **66 passed, 24 expected external/hardware
  skipped**, one third-party deprecation warning.
- Frontend Vitest: **134 passed**; TypeScript, ESLint, and production Vite/PWA
  build passed.
- Rust: **8 passed, 1 real-WGC hardware test ignored**; `cargo check` passed.
- Source diagnostics and live runtime diagnostics: **zero findings**.
- Tauri release + NSIS build passed. The launcher itself returned exit 0 and
  printed `TARS READY` only after provider and native page-load verification.

**Measured evidence (not fabricated)**:

- Kokoro samples: A `am_michael` 3.984 s / 10.057 s audio (RTF 0.396), B
  `am_onyx` 3.360 s / 9.459 s (RTF 0.355), C `bm_george` 3.731 s / 9.907 s
  (RTF 0.377). Human naturalness choice is pending.
- Current native background/noise captures show STT completion around 1.3–4.1
  seconds and are correctly classified at `wake_match`; these are not physical
  wake attempts and are not evidence of wake success.
- No physical wake, normal-provider, or hot-chart latency is claimed yet.

**Known limitations**:

- Required 20-attempt physical microphone test is pending; 19/20 reliability
  and absence of duplicated spoken responses are not yet certified.
- Early sentence TTS is prepared as complete-sentence chunks, but the multipart
  voice endpoint currently returns its final response before native playback;
  it does not yet overlap provider generation with first-sentence playback.
- The existing Claude/Codex benchmark was inconclusive. Runtime therefore keeps
  the explicitly configured `claude_code` primary and strict Codex fallback; no
  unsupported quality winner is claimed.
- Hot chart capability is preserved and regression-tested, but physical
  sub-two-second latency was not remeasured in this session.

**Exact dependencies required from other agents/users**:

- User/hardware: perform the prescribed 20 deliberate microphone attempts and
  two-stage follow-up, report any duplicate audible response, and physically
  choose A/B/C. No other agent dependency blocks this milestone.

**Next recommended action**: perform the physical test against the running
native app, then inspect `GET /api/v1/diagnostics/voice-latency` for every miss
and publish the final measured acceptance report. Do not merge this branch.

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
