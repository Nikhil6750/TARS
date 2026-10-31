# TASKS.md — TARS Task Board

Status values: `PROPOSED` (not yet authorized), `READY` (authorized,
unclaimed / ready for implementation), `IN_PROGRESS`, `BLOCKED`, `DONE`. Each agent updates the status
of its own tasks and adds detail to its own handoff file
(`docs/coordination/handoffs/<agent>.md`), not here — this file tracks
*what*, handoffs track *how it went*.

Per the Git workflow rule in `AGENTS.md`: each agent implements features
exclusively on its dedicated feature branch in its isolated worktree.
Do not work directly on `integration/v1` or `main`.

Wave 1 implementation and integration are **DONE**.

## Claude Code — `apps/backend/` (Branch: `feature/v1-backend-voice`)

- [x] DONE: Scaffold FastAPI app (`apps/backend/app/`) with Pydantic models and health endpoint.
- [x] DONE: SQLite models/migrations for conversation history, alert history, and session state (`apps/backend/storage/`).
- [x] DONE: WebSocket endpoint broadcasting `trading-event` + companion-state messages.
- [x] DONE: Mock trading-event generator (`apps/backend/events/`) emitting schema-valid events per `contracts/trading-event.schema.json`.
- [x] DONE: Voice provider interfaces (`apps/backend/voice/`): `WakeWordProvider`, `SpeechToTextProvider`, `AssistantProvider`, `TextToSpeechProvider`, each with a mock/local implementation, wired through a Pipecat pipeline.
- [x] DONE: openWakeWord adapter implementing `WakeWordProvider` (phrase "TARS", laptop-only), with push-to-talk/keyboard fallback guaranteed regardless of wake-word state.
- [x] DONE: Silero VAD integration in the Pipecat pipeline.
- [x] DONE: faster-whisper adapter implementing `SpeechToTextProvider` (local, default/required).
- [x] DONE: Fish Speech adapter implementing `TextToSpeechProvider` (local, primary), Kokoro adapter (local, lightweight fallback).
- [x] DONE: `AssistantProvider` routing (`apps/backend/assistant/`): `ClaudeCodeProvider`, `OllamaProvider`, optional `AnthropicAPIProvider`, plus deterministic-vs-model routing logic per `ARCHITECTURE.md`.
- [x] DONE: Memory layer (`apps/backend/memory/`) — SQLite FTS5 search, Obsidian vault read/index path, boundary enforcement between operational state / conversation memory / research knowledge.
- [x] DONE: APScheduler wiring for housekeeping jobs (morning/EOD summaries, journal tasks, maintenance).
- [x] DONE: OpenTelemetry instrumentation (request/tool-call/latency/error logging, no secrets).

## Codex — `tests/`, `tools/` (Branch: `feature/v1-quality-contracts`)

- [x] DONE: Contract validation harness — validate backend event output and frontend event consumption against `contracts/*.schema.json`.
- [x] DONE: Integration test scaffold across the backend/frontend WebSocket boundary.
- [x] DONE: Acceptance verification for the free/local core path — the app runs and passes all checks with zero paid keys.
- [x] DONE: Code generation tooling and verification scripts.
- [x] DONE: Black-box acceptance test suite and Playwright multi-viewport UI verification.
- [ ] PROPOSED: CI wiring (once a CI target is chosen by the coordinator).

## Antigravity — `apps/web/` (Branch: `feature/v1-web-pwa`)

- [x] DONE: Scaffold single React + TypeScript + Vite app, PWA manifest, responsive/mobile-first shell.
- [x] DONE: Tauri 2 desktop shell (`apps/web/src-tauri/`) around the same app — no separate desktop UI implementation.
- [x] DONE: WebSocket client consuming `trading-event` + companion-state updates with strict contract validation.
- [x] DONE: Active setups view (symbol, direction, entry, SL, TP, R:R, risk).
- [x] DONE: Alert history view.
- [x] DONE: Companion state/face UI (idle/listening/thinking/speaking/alert).
- [x] DONE: Chat/voice interaction surface — text input always; voice input via push-to-talk on both laptop and iPhone.
- [x] DONE: Desktop notifications via Tauri/native layer; iPhone notifications via PWA Web Push where supported.
- [x] DONE: Verify responsive behavior on laptop and iPhone viewport sizes in-browser, plus Tauri desktop configuration.

## Integration & Verification (Branch: `feature/v1-integration`)

- [x] DONE: Merge `feature/v1-backend-voice`, `feature/v1-quality-contracts`, `feature/v1-web-pwa` into `feature/v1-integration`.
- [x] DONE: Reconcile route aliases (`/health`, `/api/events`, `/api/assistant/messages`, `/api/voice/status`, `/ws`).
- [x] DONE: Implement 1 MiB payload limit middleware returning HTTP 413.
- [x] DONE: Wire frontend real API and WebSocket endpoints (`active_snapshot`, `/api/v1/assistant/query`, `/api/v1/memory/search`).
- [x] DONE: Run full 15/15 black-box acceptance test suite with zero failures.
- [x] DONE: Produce final integration handoff report.

---

# Phase 1 & Phase 2 — Market Intelligence hardening and live data

**Authorized**: 2026-10-31 by the coordinator, who also directed that these
tasks be written here. `AGENTS.md` marks this file read-only to agents, so
that direction is the override which permits this entry; it is recorded
rather than assumed. Drafted by Claude Code on branch
`docs/task-board-phase-1-2` (based on `integration/v1`), for the coordinator
to merge.

**Predecessor**: `feature/market-xray-dashboard` (pushed to origin, tip
`f7022f8`, **not merged** — the coordinator has deferred the merge). Both
phases below act on findings from that branch's release-readiness review;
see `docs/coordination/handoffs/claude.md` for the detail behind each item.

## Constraints binding every task in both phases

Non-negotiable, carried from the coordinator's instruction and `AGENTS.md`:

1. **Trading execution stays disabled.** No task here may introduce an order
   path, and no task output may suggest one exists.
2. **No additional indicators and no dashboard redesign.** These phases make
   what exists trustworthy; they do not extend it. A task that seems to need
   a new indicator is a task that has been misread.
3. **Existing companion behaviour and regression coverage are preserved.**
   The backend suite (1681 passed / 8 skipped), the frontend suite (207
   passed), the 27-check API+Chromium acceptance run and the 16-check
   desktop run are the floor, not the target. A change that breaks any of
   them is not done.
4. **`contracts/` stays read-only.** The market API is additive and outside
   `contracts/*.schema.json`. If a task appears to require a contract
   change, stop and record it in your handoff instead of editing the schema.
5. **Respect lane ownership.** Items below are assigned by the `AGENTS.md`
   ownership table. Do not fix something in another agent's directory — flag
   it in your handoff.
6. **Report honestly.** Every completed task reports: implementation
   results, tests actually run with their counts, remaining limitations, and
   the branch plus commit SHA. **Never report untested behaviour as
   verified.** "Implemented" and "verified" are different claims; if only
   a unit test exercised something, say so.

## Blockers that are not code

Four tasks below cannot be completed by an agent alone. They are marked
`BLOCKED` with the blocker named, so nobody reports them as merely pending.

| ID | Blocker | Unblocks |
|---|---|---|
| **B1** | No authenticated MT5 account on this machine (`-6 Authorization failed`). A credential/environment task for the user. | P2-C1, P2-C2 |
| **B2** | Markets are closed outside session hours; LIVE and STALE can only be observed during a real session. | P2-C2 |
| **B3** | No source CSVs under `C:\Trade Data` for USDCHF, NZDJPY, NZDCAD, NZDCHF, EURNZD, GBPNZD, AUDNZD, CADJPY, CADCHF. | P2-C4 |
| **B4** | No clean Windows machine or VM provisioned — one without the toolchain, `node_modules`, a built candle store or a warm data root. | P1-X1 |
| **B5** | Tray-icon click resists UI Automation (Windows 11 tray overflow is an XAML island); a human must click it. Pre-existing limit, recorded in `docs/demo/CAMPUS_DEMO.md`. | P1-H1 |

---

## Final directive status (2026-10-31)

Integrated branch `feature/market-xray-phase12-integration`, tip `9926aa8`. DONE below
means implemented and tested on that build; it does not mean release approved.

Evidence on the final build: backend 1819 passed, 8 skipped, 0 failed; frontend 217 passed, 31 files; API and Chromium
27/27; black-box integration 15/15; native desktop 17 of 19 with 0 failed; the 2 F11 checks were not verified by the harness, and real F11 was proven separately on this binary; backend
ownership 37/37; installer in an isolated environment 24/24; native dependency scan
clean across 558 modules.

| Open item | Why it is open |
|---|---|
| Clean-machine install (B4) | Windows 11 Home has no Sandbox or Hyper-V here. The real installer passed with PATH cut to Windows and an empty data root, on a development machine. That is not a clean machine. |
| Real LIVE and STALE (B1, B2) | No logged-in MT5 account, and the market is closed. Proven only against a fake terminal. |
| Manual tray click (B5) | Needs a person. |
| The 26-instrument target (B3) | 18 + 9 named = 27. EURCHF and GBPCHF are in neither list. No list of the 25 FX pairs exists. Coordinator decision. |
| Independent check of the tip | Codex independently verified `d8d2dcb`. The look-ahead fix, feed, first run and deadlock fix came later. |

Job Objects remain evaluation-only, as directed: not implemented.

## Implementation status — read before starting

The Claude-lane Phase 1/2 tasks (P1-C1, P1-C2, P2-C3, P2-C5) are **done and
integrated** on `feature/market-xray-phase12-integration`, accepted by the
coordinator as implementation candidates and **not release approved**. That
branch is what Antigravity and Codex should branch from, because the fields
and seams the tasks below need only exist there.

What it gives you:

| You need | It is at | Shape |
|---|---|---|
| Backend identity (P1-A1) | `GET /api/v1/health` → `identity` | `service`, `contract_version`, `instance_id`, `build_id`, `build_source`, `app_version`, `started_at` |
| The adoption rule (P1-A1, P1-A2) | `app.identity.is_compatible(service=, contract_version=)` | returns bool |
| Provenance (P2-A1, P2-X1) | every market payload → `provenance` | `source` (`RECORDED_STORE`/`LIVE_FEED`), `provider`, `observed_at`, `replay`, `is_live` |
| Replay state (P2-A1) | `GET /api/v1/market/dashboard` → `replay_pinned` | bool |
| Feed source (P2-A1) | `status.source` | same two values as `provenance.source` |

**Compatibility, not equality, is the test.** `build_id` answers "is this the
same code?", which is the wrong question: a shell that refuses every backend
differing from itself would refuse its own backend after any rebuild. Judge
with `service` + `contract_version`. `contract_version` is bumped only by a
breaking change to the API the shell depends on; adding a field does not
bump it.

## Phase 1 — Desktop release hardening

Goal: the desktop app can be released without the shell ever attaching to,
or shutting down, something it does not own.

### Claude Code — `apps/backend/` (Branch: `feature/backend-build-identity`)

- [x] DONE: **P1-C1 — Expose a backend build identity from
  `/api/v1/health`.** Add a stable identity to the existing health payload
  (`apps/backend/app/routers/health.py`): an instance id generated per
  process start, plus a build/version identifier derived from something that
  actually changes between builds (commit SHA where available, else a
  package version). Additive only — existing consumers of the payload must
  keep working, which the current frontend and `supervisor.rs` health poll
  both depend on. **This task gates P1-A1**, so land it first and tell
  Antigravity the field names in your handoff.
  *Done when*: the field is present, documented, covered by a test
  asserting it is stable within a process and differs across builds, and
  the existing health-endpoint tests still pass.

- [x] DONE: **P1-C2 — Make the market data roots configurable.**
  `market/ingest.py` hardcodes `CSV_ROOT = C:/Trade Data` and
  `DEFAULT_STORE_ROOT = D:/TARS_DATA/...` instead of reusing
  `research/config.py`'s `resolve_data_root()`, which already implements
  this repo's `TARS_DATA_ROOT` convention. Switch to the resolver and add a
  source-root override. On this machine the resolver returns the same path,
  so this is behaviour-preserving here — verify that rather than assume it.
  Also update the hardcoded store root in
  `scripts/start-market-desktop.ps1`.
  *Note*: `scripts/` is not assigned to any agent in the `AGENTS.md`
  ownership table — a genuine gap. Claude authored these launchers, so they
  are treated as Claude's here; the coordinator should settle the ownership
  question.
  *Done when*: ingest runs to the same store path with no env set, honours
  the override when set, and the ingest tests pass.

### Antigravity — `apps/web/` (Branch: `feature/desktop-supervisor-hardening`)

Read `docs/coordination/handoffs/claude.md` § "BackendSupervisor ownership
and shutdown — reviewed" before starting. It records what the review
established as already correct, so that none of it is broken by accident:
`stop()` is deliberately ownership-agnostic, and `intentional_stop` is
deliberately set before the shutdown POST.

- [x] DONE: **P1-A1 — Verify backend identity before adopting.**
  **P1-C1 is done**, so this is unblocked; the fields are in the table
  above. Adopt only a process that declares `service == "tars-backend"` and
  the `contract_version` this release expects — the same rule as
  `app.identity.is_compatible`, which exists so the shell does not have to
  restate it. `health_check_once()` in `src-tauri/src/supervisor.rs`
  currently requires only HTTP 200 from the health URL, so the shell adopts
  *any* process answering there — including an orphan from an older build,
  after which a new UI silently drives old backend code. Check the identity
  from P1-C1 before adopting, at startup and in the adopt-when-healthy
  branch of the poll loop. A mismatch must surface visibly (a health state
  the user can see) rather than being adopted quietly or triggering a
  respawn that cannot bind the port.
  *Done when*: adoption of a matching backend still works, a deliberately
  mismatched backend is refused and reported, and neither case produces the
  restart loop that `f796e1d` fixed.

- [x] DONE: **P1-A2 — Never shut down a process TARS does not own.**
  Follows from P1-A1 and is the half that matters most: `stop()` POSTs the
  shutdown endpoint regardless of ownership, which is correct for a backend
  TARS spawned or legitimately adopted, but means a foreign process on the
  port can be told to exit. Since `f796e1d`, mid-session adoption makes
  that reachable at runtime, not only at startup. Gate the cooperative
  shutdown on verified identity.
  The rule, stated so it cannot be read loosely: **a process TARS has not
  verified is a process TARS does not touch.** Not adopted, and above all
  not sent a shutdown — it may belong to somebody else. An unverified
  listener on the port is a visible, reported condition, not something to
  clear out of the way. Note that this is the one place where "do nothing"
  is the correct behaviour even though it leaves the app degraded: taking a
  stranger's process down to free a port would be worse than failing to
  start.

  *Done when*: Quit and `--quit` still leave no orphan on the port for an
  owned or legitimately adopted backend, and a foreign listener is left
  untouched. State plainly which of those two you actually tested — the
  second one is the claim that matters and the easier one to skip.

- [x] DONE: **P1-A3 — Resolve the backend port inconsistency.**
  `supervisor.rs` hardcodes `127.0.0.1:8000` in three constants while
  `chart_watcher.rs` honours `TARS_BACKEND_URL`. One mechanism, used by
  both. Consequence to fix with it: the desktop launcher's `-BackendPort`
  can currently only move where the *script* polls, never where the shell
  supervises, so the parameter cannot do what its name suggests.
  *Done when*: both modules resolve the backend address the same way, the
  default is unchanged, and the launcher parameter either works end to end
  or is removed.

- [x] DONE: **P1-A4 — Evaluate a Windows Job Object for crash cleanup.**
  The backend child is not in a Job Object with
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, so `TerminateProcess`, a crash or
  power loss orphans Python holding the port. Today that self-heals on the
  next launch via adoption — which is exactly the path P1-A1 is tightening,
  so the two interact and should be considered together. This is an
  **evaluation**: establish whether a Job Object is the right mechanism
  here, including what it would do to a *legitimately adopted* backend that
  TARS did not spawn and must not kill.
  **Evaluation only — reaffirmed by the coordinator.** Do not implement a
  Job Object under this task, even if the evaluation is favourable. It needs
  explicit approval first, because the mechanism interacts badly with the
  rule in P1-A2: a job object that kills on close would also kill a
  *legitimately adopted* backend TARS never spawned, which is precisely the
  process P1-A2 says not to touch. Say how your recommendation handles that.

  *Done when*: a written recommendation exists with the trade-off stated,
  proposed as an ADR for coordinator sign-off. No implementation.

- [x] DONE: **P1-A5 — `--market` does nothing on a cold start.** Found by
  trying it, not by reading the code. `--market` is handled only inside the
  single-instance plugin's callback, which by design fires on a *second*
  launch, so passing it to a cold start gives you the companion and no
  Market window — and no later launch can fix it, because the startup hook
  reads `TARS_OPEN_MARKET=1` rather than argv. The launcher is unaffected
  since it sets that variable, which is why this went unnoticed.
  Handle the argv on a cold start as well, in the same place the env var is
  read, so both entry points behave the same. `docs/runtime/market-xray.md`
  has been corrected to state the current limitation; update it when this
  lands.
  *Done when*: `tars-companion.exe --market` opens the window from cold and
  still focuses rather than duplicating when TARS is already running, with
  both cases actually exercised.

### Codex — `tests/`, `tools/` (Branch: `feature/clean-install-acceptance`)

- [ ] BLOCKED (**B4**, isolated run passed, clean machine not done): **P1-X1 — Test the real NSIS installer in a clean
  environment.** Everything verified so far ran against a development tree
  with its toolchain, `node_modules`, a built candle store and a warm data
  root. `TARS_1.0.0_x64-setup.exe` was produced but never installed
  anywhere lacking those. Install it on a clean Windows profile with **no
  preloaded market history**, then open Market Intelligence from the
  installed app. The interesting question is what the dashboard does with
  an empty store — it must state that plainly, not show an error panel the
  user cannot act on, and must not present an empty store as a live feed.
  *Done when*: the install, first launch, backend start, and the empty-store
  state are each recorded with evidence. Note that
  `tools/verify_market_desktop.py` and `tools/verify_market_xray.py` are in
  Codex's lane but were written by Claude under a coordinator override;
  extending them is Codex's call.

- [x] DONE: **P1-X2 — Regression gate for Phase 1.** Re-run the existing
  suites against the Phase 1 branches before any merge is proposed: backend
  pytest, frontend tests, the 27-check API+Chromium acceptance run, and
  `tools/verify_market_desktop.py` against a cold-started release binary.
  *Done when*: counts are reported as counts, and any drop from the Phase 0
  floor in constraint 3 above is named rather than averaged away.

- [x] DONE: **P1-X3 — Make fullscreen verification DPI-safe, and say when
  it could not run.** Note first what is *not* broken: fullscreen itself is
  **verified working** -- a clean run reported `1920x1080 exactly matches
  monitor 1920x1080`. The problem is that the check is unreliable across
  runs and reports its own failures as app failures. Of four runs today, two
  failed because the harness could not get the foreground and one because
  the window was minimized; only one measured anything. Its failure message
  is also actively misleading: it prints the pass-condition wording
  ("1942x1102 exactly matches monitor 1920x1080") over numbers that plainly
  do not match. Two defects behind it:
  (a) **The keystroke never arrives.** `SetForegroundWindow` on the Market
  window returns false — Windows refuses the foreground to a process that
  does not have it — so the synthetic F11 goes nowhere. A check that cannot
  deliver its input must report *"could not verify"* and not *"failed"*;
  the two mean different things to whoever reads the summary.
  (b) **DPI scaling.** `GetSystemMetrics` reported 1536x864 in one run and
  1920x1080 in another on the same display, and windowed rects include
  invisible borders (1942x1102), so comparing a window rect against a
  monitor size is unreliable. Use per-monitor-DPI-aware measurement, or
  compare against the monitor the window is actually on, rather than the
  primary metrics.
  *Done when*: the check either verifies fullscreen or reports that it could
  not, never a bare FAIL for a harness limitation, and the numbers it prints
  support the verdict it gives. A flaky check that cries wolf is worse than
  no check, because it teaches people to ignore the summary.

- [x] DONE: **P1-X4 — Investigate the intermittent minimized window.**
  Observed once: the Market window appeared at `-32000,-32000` sized
  199x34, the Windows signature of a minimized window, which silently
  invalidated three checks in that run — a minimized window cannot be
  captured (the sample held 515 distinct colours, below the 1500 threshold),
  is not desktop-sized, and ignores keystrokes. It did not reproduce on
  relaunch and no cause has been established. Two jobs, and the second
  matters more: find the cause if you can, and in any case make the
  verifier **detect** the condition and say so, because the failure
  currently presents as three unrelated check failures rather than one
  explicable state.
  *Done when*: the verifier reports a minimized window as exactly that, and
  whatever is known about the cause is written down even if the answer is
  "not reproduced, cause unknown".

### Human (the user) — not assignable to an agent

- [ ] BLOCKED (**B5**): **P1-H1 — Verify tray controls by hand.** Open the
  tray menu, click **Market Intelligence**, confirm the window opens; click
  it again with the window already open, confirm it focuses rather than
  duplicating; then use **Quit TARS** and confirm no Python process is left
  on the backend port. The handler calls the same `open_market_impl` as the
  two automated entry points, but the click itself has never been
  exercised, so it stays unverified until a human does this.

---

## Phase 2 — Live market intelligence

Goal: a real feed, with the LIVE/STALE/REPLAY distinction provably intact.
Phase 2 does not depend on Phase 1 completing, but P2-C2's credibility does
depend on P1-A1: a feed status is only as trustworthy as the backend that
reports it.

### Claude Code — `apps/backend/` (Branch: `feature/market-live-feed`)

- [ ] BLOCKED (**B1**, code done, real session unverified): **P2-C1 — Integrate MT5 through the existing
  provider interface.** Wire `monitors/mt5_provider.py` into
  `MarketXRayService` behind the current provider seam so `_feed_last_tick`
  is fed by a real authenticated session. Do not hard-code MT5 into the
  service: the provider interface stays the boundary, per the
  provider-neutrality rule in `AGENTS.md`. The no-feed path must remain
  fully working — the app still runs with nothing connected, and no paid
  API becomes a requirement of the core runtime.
  *Done when*: with a session attached the service reports from the feed;
  with none attached behaviour is byte-identical to today. Report which of
  those two you actually observed — without B1 cleared, only the second is
  testable.

- [ ] BLOCKED (**B1**, **B2**): **P2-C2 — Validate LIVE and STALE against a
  real session.** Today only REPLAY, CLOSED and the no-feed path are
  verified against reality; LIVE and STALE exist solely in unit tests. Add
  a test that a feed whose last tick ages past the freshness window
  transitions LIVE → STALE with no intervention, then observe both states
  during an open market session.
  *Done when*: the ageing transition is covered by a test **and** both
  states have been seen against a live session. Until the second half
  happens, this task is not done — report it as implemented, not verified.

- [x] DONE: **P2-C3 — Make "replay is never live" an enforced invariant.**
  The design already guarantees that only a real feed can produce LIVE and
  that stored candles never reach that code path. Phase 2 attaches a live
  provider, which is exactly what could erode it. Assert at the service
  boundary that an active replay cursor cannot emit LIVE *even with a live
  provider attached*, and carry a provider-identity/provenance field on
  every observation so a payload states which source produced it — turning
  an intention into something checkable. This task needs no feed and is not
  blocked; do it **before** P2-C1 lands, so the guard exists before the
  thing it guards against.
  *Done when*: a test attaches a live provider, sets a replay cursor, and
  asserts LIVE is unreachable; and every payload carries provenance.

- [ ] BLOCKED (**B3**, **target list undefined**): **P2-C4 — Reconcile the 26-instrument target.** The
  ceiling is source data, not code: nine pairs have no CSVs (see B3).
  Either obtain history for them, or accrue it from the live feed going
  forward and accept a short history — the second option changes what the
  strength windows can honestly compute over, so state the consequence
  rather than quietly widening coverage. Until it is closed, NZD rests on
  one pair and CHF on two.
  *Done when*: the instrument count is reported with its actual data
  backing per instrument, and any instrument added on short history is
  labelled as such.

- [x] DONE: **P2-C5 — Preserve coverage warnings and provenance.** The
  `thin_coverage` flag and the leave-one-pair-out diagnostic are what make
  uneven coverage visible; adding instruments must not weaken either.
  Closing the coverage gap silently would be worse than the gap.
  *Done when*: a test asserts the flag and the diagnostic survive an
  expanded universe, and that no instrument can enter the universe without
  declaring its data backing.

### Antigravity — `apps/web/` (Branch: `feature/market-provenance-badges`)

- [x] DONE: **P2-A1 — Surface provenance and keep the status honest in the
  UI.** Depends on P2-C3's provenance field. The dashboard already badges
  `thin_coverage` and shows the feed status; extend it to show which source
  produced the data being displayed, so a REPLAY view can never be mistaken
  for a live one at a glance. **This is not a redesign** — add provenance to
  the existing status surface and change nothing else about the layout.
  *Done when*: REPLAY, LIVE, STALE, CLOSED and NO_DATA are each visually
  distinct, provenance is shown, and the existing frontend tests pass.

### Codex — `tests/`, `tools/` (Branch: `feature/feed-status-acceptance`)

- [ ] PARTIAL (independent run predates the feed work): **P2-X1 — Independent acceptance of the feed-status
  separation.** **P2-C3 is done**, so this is unblocked, and the fields are
  in the table at the top. Verify it from outside the backend rather than
  trusting its own tests — the backend asserting its own honesty is exactly
  the claim an independent check is for: drive the API with a replay cursor set and assert no
  response anywhere reports LIVE; assert every observation carries
  provenance. An independent check is the point — this is the invariant the
  whole feature's honesty rests on.
  *Done when*: the check runs against a live backend and fails loudly if a
  replayed observation is ever labelled live.

- [x] DONE: **P2-X2 — Regression gate for Phase 2.** As P1-X2, re-run
  against the Phase 2 branches before any merge is proposed.

- [ ] PARTIAL (independent run covers `d8d2dcb`, not the tip): **P2-X3 — Independent integration testing of
  `feature/market-xray-phase12-integration`.** The Claude-lane work was
  verified by the agent that wrote it, which is the weakest form of
  evidence. Test the integrated branch end to end without reusing its
  tests: drive the real HTTP API and the desktop app, and check at minimum
  that the health identity is well formed and compatible, that no payload
  anywhere reports a replayed observation as live, that every instrument
  declares its data backing, that the thin-coverage flags for NZD and CHF
  are still present and still true, and that the dashboard still paints
  against the real store. Where a check cannot run, say so rather than
  omitting it.
  *Done when*: a pass/fail list exists that was not derived from
  `apps/backend/tests/`, with counts.

---

## Coordinator actions outstanding

- **Merge decision** on `feature/market-xray-phase12-integration`, which
  now contains `feature/market-xray-dashboard`, both Claude Phase 1/2
  branches and this board. Accepted as implementation candidates, **not
  release approved**; nothing is merged to `integration/v1` or `main`.
- **Release approval** is blocked on evidence that does not exist yet:
  clean install (B4), real-feed LIVE/STALE (B1, B2) and a human tray click
  (B5). None of the three may be recorded as done without it.
- **`CURRENT_STATE.md` is stale** — it predates the Market X-Ray feature and
  does not mention it. Agents are told to read it second, so it is actively
  misleading right now. Only the coordinator can update it.
- **`scripts/` has no owner** in the `AGENTS.md` ownership table (see
  P1-C2).
- **Sign-off requested** on the `apps/web/` ownership exception recorded in
  `docs/coordination/handoffs/claude.md`, and on P1-A4's ADR if that
  evaluation recommends a Job Object.

