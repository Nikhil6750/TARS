# Handoff — Claude Code

Owned directory: `apps/backend/`. Only Claude Code edits this file. See
[AGENTS.md](../../../AGENTS.md) for the full handoff protocol.

Update this file at the end of every session working on `apps/backend/`, using
the template below. Keep only the latest handoff at the top; older entries
may be kept below a `---` separator if useful for history, but are not
required reading for the next session (`CURRENT_STATE.md` is authoritative
for that).

---

## Latest handoff — WebSocket send-after-close (2026-10-31)

**Branch**: `fix/market-ws-send-after-close`, from `7fe5875`
**Fix SHA**: `6c1feb6`
**Status**: fixed and tested. Not merged. Depends on nothing else.

Reported by Codex's independent verification of `7fe5875`, reproduced by
`tests/integration/test_market_ws_disconnect.py` on
`feature/market-xray-final-verification`.

**Root cause.** The sender awaited a multi-second frame build in a thread, then wrote
without checking anything. A client dropping during that await closed uvicorn's side and
set the stop event, but the sender was suspended and never looked. Starlette still
considered its side open, so the write reached uvicorn, which raised a plain
`RuntimeError`. The sender only caught `WebSocketDisconnect`, so it escaped as an ASGI
exception.

**Fix.** `apps/backend/app/routers/market_xray.py` only, plus tests:

* `_MarketSocket` owns every write and close: serialised under a lock, checked inside it,
  closes once, and turns only a confirmed closed-socket failure into a normal end.
* The sender waits on the build and the stop signal together. `to_thread` cannot be
  cancelled, so a build in progress is waited for, bounded by `FRAME_DRAIN_SECONDS`.
* The receiver no longer swallows `RuntimeError` and `Exception`. Disconnect ends it,
  an unreadable message or invalid command closes 1008, a bug closes 1011 and re-raises.

**Behaviour changes, all at the edges.** A bad client message now gets an explicit 1008
where the connection used to end with no close frame. `replay.step` and `replay.reset` on
an empty store are ignored and no longer end the connection. Frames are unchanged.

**Evidence.**

| Check | Result |
|---|---|
| Codex reproduction, marker disabled | failed before, passes after |
| `tests/test_market_ws_lifecycle.py` | 21 of 27 failed before; 27 pass after; 10 repeat runs clean |
| Real backend, 120 mid-stream disconnects (4 in 5 aborted) | before: 120 send-after-close errors. After: 0, and 0 tracebacks |
| After that stress | correct REPLAY/RECORDED_STORE frame, `is_live` false, 0 connections left, DISABLED |
| Backend suite | 1846 passed, 8 skipped, 0 failed |
| Integration | 15/15 |
| API and Chromium | 27/27 on 3 of 4 runs. The first exited 1 straight after the backend suite, cause not captured; the same suite timed out under load once on `7fe5875` |

**For Codex.** The reproduction now reports XPASS. Remove its `xfail` marker. It is not on
this branch: its file is yours, so I ran an unmodified copy and removed it again.

**Known limitations.**

* Closed-socket detection matches message text, because neither uvicorn nor Starlette has a
  type for it. An upgrade that rewords it would turn a quiet end into a logged error, which
  the real-server tests would catch. Nothing would go unreported.
* Builds still run after their client has gone, because a thread cannot be stopped. After
  this stress a new client waited 97s for its first frame while 120 abandoned builds
  finished. It needs a reconnect storm to matter, and it was already true. Sharing one
  in-flight build between clients would remove it, and is a separate change.
* A second cancellation during shutdown can abandon the wait for a build.
* A build that runs past `FRAME_DRAIN_SECONDS` is logged and left, since it cannot be killed.

### Next recommended action

Merge this into the integrated branch with `feature/market-xray-final-verification`, then
Codex removes the marker and reruns final verification. The release checks listed in the
previous entry remain open.

## Previous handoff — Market Intelligence final implementation directive (2026-10-31)

**Branch**: `feature/market-xray-phase12-integration`
**Final SHA**: `9926aa8`
**Status**: engineering complete on one build; **not release ready**. See the task
board's "Final directive status" for what stays open.

### Corrections to my earlier reports

Each of these was reported as true and was not. They change what earlier entries in
this file can be relied on for.

1. **Replay had look-ahead.** I reported "no look-ahead" as guaranteed from the first
   session. `ts` is a bar's open, so `ts <= as_of` returned the bar still forming at
   the cursor, with its later prices. Fixed in `d99aa5f`; 9 new tests fail on the old
   filter. Calculation version is now `xray-v2`.
2. **P2-C3 did not enforce what I said.** With a feed attached and no pin the service
   returned LIVE and `is_live: true` on panels computed from old recorded bars, and
   `is_live` was also true for STALE and CLOSED. Fixed in `3dff01f`.
3. **Every "port 8000 free" from my `netstat | grep` was meaningless.** The pattern
   expected the port after LISTENING; Windows prints it before. It could never match,
   which is also why a `taskkill` loop silently did nothing and a stray uvicorn
   survived. The psutil results from the verifiers were real.
4. **`d8d2dcb` says no Antigravity runtime is reachable.** Too strong: it was running in
   this checkout. I could not message it, which is a different thing.
5. **The "26 FX names" test encoded a wrong target.** The target is 25 FX plus XAUUSD.
6. **`ec69127` blames LF line endings for the installer script stopping.** A guess, and
   wrong. `python` is a `.cmd` shim and needed `call` (`9926aa8`).

### Who did what

Several agents worked in this checkout at once, without a channel between them.

* **Claude Code**: ownership, supervisor rewrite, endpoint, deadlock, installer, first
  run, feed, look-ahead, and everything committed under this entry.
* **Antigravity**: provenance UI (`cd2ac48`, committed by me unchanged because it sat
  uncommitted and idle, marked as not my work), F11 listeners, the launcher `--market`
  argument, its handoff, and a new branch that moved this checkout's HEAD.
* **Codex**: independent verification and the hardened verifier, on its own branch, at
  `d8d2dcb`.

`docs/coordination/handoffs/antigravity.md` overstates this and should be corrected by
its owner: it claims `GET /api/v1/runtime/identity` and a `BackendDecision` type, neither
of which exists, and it lists the supervisor and endpoint work as its own. Codex's
handoff likewise calls `d8d2dcb` an Antigravity commit. I have not edited either file.

`AllowSetForegroundWindow(ASFW_ANY)` reached a release build swept into my `b6241d0`
along with uncommitted edits to `lib.rs`. It let any process take the foreground on every
Market focus event, seemingly to help a harness land a keystroke. Removed in `c5478f6`.
That is a change to Antigravity's code, so it is flagged here. Real F11 was then proven
on the final binary without it.

### Defects found by running things, all with regression coverage

* Replay look-ahead; LIVE without live bars; `is_live` on stale data.
* Main-thread window deadlock: two launches a second apart hung the app, and every later
  launch including `--quit` queued behind it.
* An installed app had no backend at all.
* `storage/` excluded from the bundle although it is also a code package.
* Post-import reload dropped a custom store root, and `/setup` could pair DONE with the
  old empty store.
* A live child that stopped answering was replaced without being killed, and `restart()`
  could race the poll loop into a double spawn.
* The WebSocket built a second service, kept a stale one after an import, and never sent
  the provenance the UI reads.
* The Market window opened larger than a 1920x1080 screen at 125%.
* The zone database lived in `apps/backend/apps/backend/storage`. Copied on first start,
  original left in place.

### Live feed: what is and is not proven

The bridge, freshness, broker-time handling, bar construction, disconnect and recovery run
through the real provider, bridge, service and store against a fake `MetaTrader5` module.
No real terminal has ever been connected: `-6 Authorization failed`, and it is Saturday.

Assumptions a real session must check: the broker-offset estimate, spread units on rates,
and whether symbol names need `TARS_MT5_SYMBOL_SUFFIX`.

### Known limitations

* Installed voice runs on mock providers until configured, and the mock trading-event
  generator runs by default, as it does in a checkout.
* A hard kill still leaves the backend running; the next launch reclaims it.
* A backend answering slowly can make one reclaim check miss. Seen once, in an early run
  of `orphan_reclaim`, and not since a longer wait.
* The minimized launch was seen once and never reproduced. Cause unknown.
* `verify_market_desktop.py` reports F11 as NOT VERIFIED when Windows refuses the
  foreground. F11 itself was proven separately, with a real click then a real key.
* Live bars are in memory and refill from the terminal after a restart.
* The browser suite passed 27/27 on 3 of 4 runs on this tree. The fourth timed out
  loading a page while the machine ran other suites, so it is load-sensitive.
* Pre-existing lint, unchanged: 3 ruff errors in `app/main.py`, `routers/realtime.py`,
  `routers/voice.py`; 1 eslint error in `src/test/orb-native.test.ts`.
* Two uvicorn processes of mine from earlier today remain on ports 8018 and 8019. The
  permission classifier refused to stop them. They use about 0.1 GB and are harmless.

### Dependencies

* **Coordinator**: the 25-pair list; merge and release decisions; a clean Windows machine;
  a logged-in MT5 account; a person to click the tray.
* **Antigravity**: correct its handoff; review the `lib.rs` changes.
* **Codex**: rerun independent verification against this tip.

### Next recommended action

Run `tools/verify_installer.py`, then the manual checks, on a clean Windows machine.

## Previous handoff — Phase 1 & 2, Claude-lane READY tasks (2026-10-31)

Four tasks from `docs/coordination/TASKS.md`, all in `apps/backend/`. The
coordinator authorized implementation and asked for the priority order
"backend ownership safety and replay/live isolation", which is why P1-C1 and
P2-C3 were done first and P1-C2 last.

All four were implemented on two task branches and are now **integrated on
`feature/market-xray-phase12-integration`**, which the coordinator accepted as
implementation candidates and explicitly did **not** release approve. Nothing
is merged into `integration/v1` or `main`.

| Branch | Tasks | Commits |
|---|---|---|
| `feature/backend-build-identity` | P1-C1, P1-C2 | `5fd0921`, `333c609`, docs `ddf5606` |
| `feature/market-live-feed` | P2-C3, P2-C5 | `75e3ff5`, `4fee2d0` |
| `feature/market-xray-phase12-integration` | both of the above, plus the compatibility work below and the task board | see "Integration" at the end of this entry |

All branch from `feature/market-xray-dashboard` (`f7022f8`) rather than
`integration/v1`, since every task needs the Market X-Ray code the
coordinator asked these branches to include.

The handoff conflict anticipated in the previous version of this note did not
materialise: the combined entry only ever existed on the P1 branch, so both
merges applied cleanly and there was nothing to reconcile by hand.

### P1-C1 — backend build identity — IMPLEMENTED, tests pass

**Commit**: `5fd0921`

`/api/v1/health` now carries an `identity` block: `instance_id` (uuid4 prefix
generated once at import, so it is stable within a process and unique across
processes), `build_id`, `build_source`, `app_version`, `started_at`.
`build_id` is the commit SHA where the repository is readable, since that is
the only value that changes whenever the code does, falling back to the
release version in an installed build; `build_source` says which of the two
it is, so a version can never be read as a commit. `TARS_BUILD_ID` overrides
both, for packaging to stamp what it produced.

New file `apps/backend/app/identity.py`. `HealthResponse.identity` defaults to
`{}`, so every existing construction keeps working. The git call is cached
for the process lifetime: re-reading HEAD after a checkout would report code
this process is not running.

Identity never raises. The supervisor polls health every 5 seconds, and
failing that poll over a missing `git` would be worse than an unknown build
id.

**Tests**: 13 new in `tests/test_identity.py` — instance ids that genuinely
differ across two spawned interpreters, build ids that differ across
commits, the version fallback, the env override, a broken-`git` case, and a
drift test that fails if `APP_VERSION` and `tauri.conf.json` disagree. All
pass. Observed on this machine: `build_id` `f7022f80e524`, `build_source`
`git`.

**Limitations — not verified**:

1. **No consumer exists yet.** This serves an identity; nothing checks it.
   "A supervisor refuses a mismatched backend" is P1-A1's claim to make, not
   one this task has demonstrated.
2. **The `version` fallback has never run in a real installed build.** It is
   proven by substituting the git lookup, not by an install without a
   repository — that is P1-X1, blocked on B4.

**Dependency status**: **P1-A1 is unblocked.** Field names are
`identity.instance_id`, `identity.build_id`, `identity.build_source`,
`identity.app_version`, `identity.started_at`, all strings, all at the top
level of the health payload under `identity`.

### P1-C2 — resolved data roots — IMPLEMENTED, tests pass

**Commit**: `333c609`

`market/ingest.py` no longer hardcodes its roots. The store is
`research.config.resolve_data_root() / "market" / "xray_store"`, reusing this
repo's existing `TARS_DATA_ROOT` convention instead of restating it, so the
market store follows the same rule as every other dataset including its
refusal to fill the system drive. The CSV source root is overridable with
`TARS_MARKET_CSV_ROOT` and otherwise falls back to `C:\Trade Data`. Both
launchers now ask the backend where the store is rather than naming the path
themselves.

**Tests**: 9 new in `tests/test_market_roots.py`, all pass. Verified
behaviour-preserving on this machine by direct comparison: the resolver
returns `D:\TARS_DATA\market\xray_store`, identical to the replaced
literal, with the existing `manifest.json` present — so the ingested 5.2M
bars were not orphaned. Overrides exercised: `TARS_DATA_ROOT` and
`TARS_MARKET_CSV_ROOT` both redirect. The launchers' new resolution step was
executed in PowerShell and returns the same path with the manifest found.

**Limitations — not verified**:

1. **Only verified where the resolver returns `D:`.** On a machine without a
   suitable non-system drive, `resolve_data_root()` falls back inside the
   repo and the store would land somewhere else. Correct by design, but no
   such machine was available; the test for it skips rather than pretends.
2. **The launchers were not re-run end to end.** Only the line that changed
   was executed. A full run rebuilds or launches the desktop app.

**Dependency status**: none. Does not block or depend on another task.

### P2-C3 — replay is never live — IMPLEMENTED, tests pass

**Commit**: `75e3ff5`

Done deliberately **before** P2-C1, so the guard exists before the thing it
guards against. Until now the rule held partly by accident: there was no
feed to attach, so nothing could compete with the replay cursor. With a feed
attached and the cursor rewound, the old precedence in `market_status()`
would have reported LIVE while every panel was still computed at
`ts <= cursor` — replayed data labelled live.

`clock.resolve_status` is now the single gate, and the only caller of
`live_status`. Precedence: a pinned replay cursor beats any attached feed; no
feed means recorded history; only then may a real feed be judged LIVE or
STALE. `MarketXRayService` gains an explicit `_replay_pinned` flag (set by
moving the cursor, cleared by `reset_cursor`, because returning to the live
edge is the request to stop replaying) and `attach_feed`/`detach_feed` as the
seam a real adapter uses. It also fails closed: a status that somehow
resolved LIVE while pinned is downgraded and logged as a bug rather than
shown.

Every payload now carries `provenance` — `source` (`RECORDED_STORE` or
`LIVE_FEED`), `provider`, `observed_at`, `replay`, `is_live` — so a client
or a test can check what it is looking at instead of trusting a label.
`_resolve_status` splits the decision from the write, so stamping eight
panels does not persist eight feed-health rows.

**Tests**: 28 new in `tests/test_market_live_isolation.py`, all pass — the
pure gate swept across the freshness boundary (the pin wins at every tick
age from 0s to 24h), a guard that `live_status` keeps exactly one call site
so nobody can route around the gate, and the headline case through the real
service and the real candle store: attach a healthy feed, rewind, and
nothing reports LIVE. Verified end to end over HTTP as well: the dashboard
returns `REPLAY`/`RECORDED_STORE` with `replay_pinned` true and every nested
panel declaring `is_live: false`.

**Limitations — not verified**:

1. **No real feed has ever been attached.** `attach_feed` is exercised with
   a synthetic provider name and timestamp. The invariant is proven against
   that, not against MT5 — P2-C1, blocked on B1.
2. **LIVE and STALE remain unobserved in reality.** The gate's LIVE branch is
   covered by unit test only; B1 and B2 still stand (P2-C2).
3. The frontend does not read `provenance` yet — that is P2-A1, in
   Antigravity's lane.

**Dependency status**: **P2-X1 and P2-A1 are unblocked** — the provenance
field they both need now exists and is served on every market endpoint.
P2-C1 now has `attach_feed` as its seam.

### P2-C5 — coverage warnings survive expansion — IMPLEMENTED, tests pass

**Commit**: `4fee2d0`

`Instrument.backing` is now required with no default, so an instrument
cannot enter the universe without declaring what its numbers rest on
(`RECORDED_CSV` or `LIVE_ACCRUED`). The distinction bites the moment P2-C4
grows coverage from a live feed: a pair accrued tick by tick is short by
construction, and a strength window over it is not comparable to one over
years of recorded bars. `UNAVAILABLE_PAIRS` declares the nine pairs with no
source data here rather than leaving them in prose, and the strength payload
reports `pairs_unavailable` (absent from this machine) separately from
`pairs_missing` (absent from this window), because conflating them would
hide a permanent gap behind a transient one.

**Tests**: 15 new in `tests/test_market_coverage.py`, all pass — over
today's universe and a simulated 26-pair one. The flag clears only where
pairs were genuinely added, still fires for a currency that is thin inside a
large universe, and the leave-one-out diagnostic still singles out a
dominating pair. Two of my expectations were wrong first time and the code
was right: NZD gains six pairs rather than four, and the stub's volatility
had to be made comparable to its drift, because otherwise every pair
saturated the ±6 clamp in `normalized_return` and no pair could dominate.

**Limitations — not verified**: the expanded universe is simulated by
monkeypatching, with a stub store. Real coverage expansion is P2-C4, blocked
on B3 — nine pairs have no source CSVs. Nothing here obtains data.

### Regression

| Suite | Result |
|---|---|
| Backend, `feature/backend-build-identity` | **1703 passed, 8 skipped, 0 failed** (1681 baseline + 13 + 9) |
| Backend, `feature/market-live-feed` | **1724 passed, 8 skipped, 0 failed** (1681 baseline + 28 + 15) |
| ruff / mypy on all changed files | clean |
| Native desktop verification | **17/18** (see below) |

Trading execution remains disabled; no task touched it, and the existing
health assertion that `live_trading_enabled` is false still passes.

The desktop app was launched and verified against the modified backend, to
check the coordinator's requirement that existing companion and dashboard
behaviour is preserved. It is: the launcher resolved the store root through
the new P1-C2 path and found 18 instruments, the backend came up healthy
reporting REPLAY, exactly one supervised backend ran as a child of the
shell, closing the Market window left the companion running, `--market`
reopened it without a second instance, and a cooperative quit left no orphan
on port 8000.

Three things came out of that run which are worth recording precisely,
because two of them are about the harness and one is a real gap:

1. **F11 fullscreen is unverified today, not broken.** The check failed in
   both runs. Diagnosed rather than guessed: `SetForegroundWindow` on the
   Market window returns false, so Windows refuses to give the test process
   the foreground and the synthetic F11 never reaches the window. The same
   check passed in the previous session, and `set_fullscreen` is untouched
   on these branches, so there is no evidence of a regression -- but equally
   no evidence it works today, and it is reported as neither.
   Also surfaced: `GetSystemMetrics` reported the monitor as 1536x864 in one
   run and 1920x1080 in another, which is DPI scaling, and it makes the
   verifier's window-versus-monitor comparison unreliable. That is in
   Codex's lane (`tools/`).
2. **`tars-companion.exe --market` does nothing on a cold start.** A real
   gap, found by trying it: `--market` is handled only in the
   single-instance plugin's callback, which fires on a *second* launch, so
   passing it to a cold start gives the companion and no Market window. The
   startup hook reads `TARS_OPEN_MARKET=1` instead, which is what the
   launcher sets, so the launcher is unaffected. `docs/runtime/market-xray.md`
   advertised `--market` without that condition and is corrected here. The
   code gap is in `apps/web/src-tauri/src/lib.rs`, i.e. **Antigravity's
   lane** -- handling the argv on a cold start as well would close it.
3. **The Market window came up minimized once**, at `-32000,-32000` sized
   199x34, which invalidated three checks in that run: a minimized window
   cannot be captured (a blank 515-colour sample), is not desktop-sized, and
   ignores a keystroke. It did not reproduce on relaunch, where the window
   opened normal and visible, and the re-run passed those checks. Recorded
   as intermittent rather than dismissed, since an app that occasionally
   starts minimized would be a genuine annoyance; nobody has established a
   cause.

### What no task here verified

The honest summary: all four tasks are **implemented with passing tests**,
and **none** is verified against the real-world condition that motivated it.
Nothing checks the identity yet (P1-A1), no real feed has been attached
(B1), LIVE and STALE have still never been observed during an open market
(B2), no pair has actually been added (B3), and no clean install has been
tried (B4). Those five remain exactly as open as the task board says.

### Next recommended action

Antigravity can start **P1-A1** now; the field names are listed above. In
this lane the next unblocked work is nothing — every remaining Claude-lane
task (P2-C1, P2-C2, P2-C4) is blocked on B1, B2 or B3, which are
credential, calendar and data-acquisition problems rather than code. The
most useful thing a coordinator can do to unblock this lane is authenticate
an MT5 account (B1).

### Integration — `feature/market-xray-phase12-integration`

Built by merging both task branches onto the Market X-Ray line with
`--no-ff`, so each task's history stays legible. Both merges were clean.
Market X-Ray functionality is preserved: nothing was reverted, and the
desktop app was launched and exercised against the integrated backend.

One addition was made on the integration branch itself, for the
coordinator's requirement that **adoption verifies compatibility and never
assumes ownership of an arbitrary healthy process**:

* `identity` now also carries `service` (a fixed `"tars-backend"` marker)
  and `contract_version`, and `app.identity.is_compatible()` states the
  adoption rule next to the values it judges.
* The reasoning, because it changes what P1-A1 should implement: `build_id`
  answers "is this the same code?", which is the wrong question. A shell
  that refused every backend differing from itself would refuse its own
  backend after any rebuild. Compatibility has to be a separate, coarser
  check — hence a contract number bumped only by a breaking change, never
  by an added field.
* `service` is the part that addresses "arbitrary healthy process": a 200
  from a port proves almost nothing, and an unrelated server can serve
  `/api/v1/health`. Finding this marker is how the shell knows a TARS
  backend answered. P1-A2 now states the rule plainly — a process TARS has
  not verified is one TARS does not touch, not adopted and above all not
  sent a shutdown, since it may belong to somebody else.

Pre-existing lint not touched: `ruff` reports 3 errors in `app/main.py`,
`app/routers/realtime.py` and `app/routers/voice.py`. All three are present
on the base commit and in files this integration does not modify, so they
were left alone rather than mixed into this work. Every file this branch
does change is clean under `ruff` and `mypy`.

### Combined regression on the integrated branch

| Suite | Result |
|---|---|
| Backend pytest | **1759 passed, 8 skipped, 0 failed** |
| Frontend (vitest) | **207 passed**, 29 files |
| API + Chromium acceptance | **27/27** |
| Native desktop verification | **18/18** |
| `tsc` | clean |
| `ruff` / `mypy` on all 14 changed files | clean |

1759 is 1681 baseline + 13 (P1-C1) + 9 (P1-C2) + 28 (P2-C3) + 15 (P2-C5) +
13 (compatibility), which accounts for every test added and leaves none of
the baseline unexplained.

**F11 fullscreen is now verified**, correcting what the previous entry
recorded as unverified: the desktop run reported `1920x1080 exactly matches
monitor 1920x1080`. The mechanism was always fine; two earlier runs failed
because the harness could not get the foreground, and one because the window
was minimized. So fullscreen works, and the harness is unreliable across
runs -- which is why P1-X3 and P1-X4 still stand as Codex tasks.

The acceptance run also confirms the Phase 2 work did not disturb what was
already true: currency strength still ranks all eight, `thin=['NZD', 'CHF']`
is still flagged, and the leave-one-pair-out diagnostic is still present on
every currency.

Pre-existing lint, left alone rather than mixed into this work: 3 `ruff`
errors (`app/main.py`, `app/routers/realtime.py`, `app/routers/voice.py`)
and 1 `eslint` error (`src/test/orb-native.test.ts`). All are present on the
base commit, in files this branch does not modify. This also corrects an
over-broad claim in the previous entry: "eslint clean" was true of the
`marketxray` subtree, not of all of `src`.

### The adoption risk, observed rather than theorised

Worth recording because it is the exact failure coordinator point 5 is
about, and it happened by accident. On the first desktop launch of this
verification the shell reported a healthy backend -- but the process serving
port 8000 was a `python -m uvicorn` instance left over from the browser
acceptance run, **not** the shell's own `python run.py` child. The shell
adopted it purely because it answered 200 on the port, and inherited that
process's replay cursor, so the dashboard opened at 08:55 instead of the
newest recorded bar.

It looked like a cursor bug. It was not: `store_bounds("M5")` returns
20:55, and after killing the stray listener a shell-spawned backend started
at 20:55 as designed. The real defect is that nothing distinguished the two
processes.

Note what the new fields would and would not have changed here. That
leftover *was* a TARS backend on the same contract, so `is_compatible`
would have adopted it too -- correctly, since adopting a compatible backend
is the documented behaviour. What `service` and `contract_version` prevent
is adopting a process that is **not** a compatible TARS backend, which is
the case that silently runs a new frontend against old code or takes
ownership of something belonging to somebody else. Distinguishing two
compatible TARS backends is what `instance_id` is for, and using it to
notice "this is not the child I started" is P1-A1's call to make.

### Assignments prepared on this branch

The task board now carries an "Implementation status" table naming the exact
fields Antigravity and Codex need, since the earlier assignments were
written before the backend half existed. Four tasks were added from defects
found by running things rather than reading them:

* **P1-A5** (Antigravity) — `--market` does nothing on a cold start.
* **P1-X3** (Codex) — fullscreen verification is neither DPI-safe nor able
  to distinguish "could not verify" from "failed".
* **P1-X4** (Codex) — the intermittent minimized window, and making the
  verifier detect that state instead of reporting three unrelated failures.
* **P2-X3** (Codex) — independent integration testing of this branch,
  explicitly not reusing `apps/backend/tests/`, because work verified by the
  agent that wrote it is the weakest form of evidence.

P1-A4 is restated as **evaluation only**, per the coordinator, with the
reason spelled out: a kill-on-close job object would also kill a
legitimately adopted backend, which is the exact process P1-A2 says not to
touch.

## Previous handoff — Market X-Ray market-intelligence feature (2026-10-31)

**Branch**: `feature/market-xray-dashboard`
**Base SHA**: `db49310` (Phase 3E, `feature/tars-desktop-demo-20260920`)
**Final SHA**: engine + dashboard `26de505`; docs `f87d62a`; desktop
integration and the three defects below `f796e1d`; SHA record `b34cacf`;
release-readiness review and pending-verification records: the commit that
adds the section below.

**Pushed**: to `origin/feature/market-xray-dashboard`. Not merged into
`main` or `integration/v1`.

Branched from `db49310` rather than `integration/v1` because the feature
reuses Phase 3E backend infrastructure that only exists on that line. No
existing file was rewritten; `apps/web/` was touched outside Claude's normal
lane under explicit coordinator direction (see "Ownership note" below).

### Work completed

A deterministic Forex/XAUUSD market-intelligence feature. Everything it
displays is computed from recorded bid/ask candles on this machine.

* `apps/backend/market/` — new package: `universe` (the 18 data-backed
  instruments), `ingest` (bid/ask CSV → mid-price Parquet via DuckDB),
  `store` (as-of-bounded reads), `indicators` (Wilder ATR, normalised
  returns, pivots, regime), `strength` (8-currency ranking with coverage +
  leave-one-pair-out), `orderblocks` (displacement/origin/BOS detection and
  zone lifecycle), `xray` (the X-Ray series), `state` (SQLite persistence),
  `clock` (sessions, feed status, replay), `service` (orchestration).
* `apps/backend/app/routers/market_xray.py` — HTTP + WebSocket API under
  `/api/v1/market`, registered in `app/main.py`.
* `apps/web/src/marketxray/` + `apps/web/market.html` — the dashboard, built
  as a second Vite entry so the companion app at `index.html` is untouched.
* `apps/web/src-tauri/src/lib.rs` — a `market` window, tray item, the
  `open_market_intelligence` / `market_toggle_fullscreen` commands, and
  `--market` / `--quit` routed through the single-instance plugin.
* `scripts/start-market-xray.{ps1,cmd}` (browser) and
  `scripts/start-market-desktop.{ps1,cmd}` (Tauri desktop; builds the
  release binary on first run, `-Dev` for the Vite workflow).
* `tools/verify_market_xray.py` — API + Chromium acceptance verification.
* `tools/verify_market_desktop.py` — native desktop verification.
* `apps/backend/tests/test_market_xray.py` — 42 unit tests.

### Two defects found by running the desktop app, and fixed

Both were found only by launching the real application; neither showed up
in any unit test or in the browser build.

1. **The market endpoints blocked the event loop** (mine, a regression).
   Every handler in `app/routers/market_xray.py` was `async def` while
   doing synchronous DuckDB scans and order-block detection — roughly two
   seconds of CPU for an 18-instrument `leaders()` call. On the event loop
   that stalled every other request, including the desktop shell's backend
   health poll. The supervisor concluded the backend had died and restarted
   it; the replacement could not bind port 8000 because the original was
   still serving, so it exited, which looked like another crash — an
   endless 2-second respawn loop, visible in the console as repeated
   `[Errno 10048] only one usage of each socket address`. Fixed by
   declaring the handlers `def` (Starlette then runs them in its
   threadpool) and offloading the WebSocket frame with
   `asyncio.to_thread`; the push interval also went 5s → 10s, since each
   frame is real CPU.
2. **Close-to-tray applied to every window** (pre-existing, exposed by
   adding a second window). `on_window_event` in `lib.rs` called
   `api.prevent_close()` unconditionally, so closing the Market window
   left it alive-but-hidden *and* collapsed the companion into its orb as
   a side effect. Now scoped: the market window closes normally, the
   companion keeps its close-to-tray behaviour.
3. **The window parked on an error panel at cold start** (mine). The shell
   opens the Market window and starts the backend at the same moment, so
   the first fetch reliably lost that race and the dashboard sat on
   "Cannot reach the TARS backend" until someone pressed retry — found by
   looking at a verification screenshot, not by any assertion, which is
   why the desktop verifier now requires a *populated* dashboard (measured
   by distinct colours: ~450 for the error panel, ~3000 once painted)
   rather than merely a non-blank window. The dashboard now retries on its
   own with backoff and says so.

Also hardened, in `supervisor.rs`: the poll loop treated "our child handle
exited" as "backend down" even when the health check was passing, which is
what turned one transient blip into the respawn loop above. It now adopts
the healthy backend and clears the stale handle — exactly the
adopt-or-spawn rule the module already applies at startup. This is a
change in Antigravity's lane; it is small, matches the module's documented
intent, and is flagged below for review.

### Interfaces exposed

`GET /api/v1/market/{health,instruments,dashboard,xray,strength,leaders,
config,events}`, `GET /api/v1/market/{candles,order-blocks}/{symbol}`,
`GET /api/v1/market/strength/{currency}/history`,
`POST /api/v1/market/replay/{seek,step,reset}`, and
`WS /api/v1/market/ws` pushing `market.frame` every 5s.

### Tests run

* 42 new unit tests pass (`tests/test_market_xray.py`).
* 27/27 acceptance checks pass (`tools/verify_market_xray.py`) against the
  supervised backend on port 8000, covering real candles, X-Ray pixels,
  replay determinism, event dedupe, hover, symbol selection, timeframe
  switching and backend-loss handling.
* **16/16 desktop checks pass against the real release binary**
  (`tools/verify_market_desktop.py`): the Market window exists inside
  `tars-companion.exe` at 1942x1102, its pixels captured with
  `PrintWindow`; F11 enters fullscreen (exactly 1920x1080, the monitor)
  and restores; the window resizes; closing it leaves the companion and
  the backend running; `--market` reopens it without a second instance;
  exactly one supervised backend runs as a child of the shell; and the
  status reads REPLAY, never LIVE.
* Cooperative shutdown verified: `tars-companion.exe --quit` leaves no
  shell process and nothing listening on port 8000. (A hard
  `TerminateProcess` on the shell *does* orphan the backend — expected,
  and the reason `--quit` exists and is what the test drives.)
* Currency strength cross-checked against an independent NumPy/pandas
  implementation — identical to 3 decimals.
* Build: `cargo check` clean; `npm run tauri build` produced
  `target/release/tars-companion.exe` (13.4 MB) and
  `bundle/nsis/TARS_1.0.0_x64-setup.exe`.
* Regression after the fixes: backend `1681 passed, 8 skipped, 0 failed`;
  frontend `207 passed`. ruff + mypy + tsc + eslint clean on all new code.
* Toolchain installed this session: VS Build Tools (VCTools workload) →
  `C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools`, MSVC
  14.51.36231, Windows SDK 10.0.26100. Note the path is `18`, not `2022` —
  the launcher resolves it via `vswhere -requires`, not a hardcoded year.

### Known limitations

1. **Tray-icon click is not automated.** Windows 11's tray overflow is an
   XAML island that resists UI Automation, which is the same gap
   `docs/demo/CAMPUS_DEMO.md` already records for this repo ("Tray icon
   click and the tray menu are implemented but not automated; verify by
   eye"). The tray item is registered and its handler
   calls `open_market_impl` — the identical function exercised by the two
   verified entry points (`TARS_OPEN_MARKET=1` at startup, and `--market`
   through the single-instance plugin) — but the click itself was not driven
   by a test.
2. **No live feed.** MT5 returns `-6 Authorization failed` (no logged-in
   account) and the market is closed. The dashboard runs in REPLAY off
   recorded history ending 2026-09-14 (XAUUSD 2026-09-11). The MT5 adapter
   path exists in `monitors/mt5_provider.py` but is not wired into the market
   service — `_feed_last_tick` stays `None`, which is what keeps the status
   out of LIVE. Wiring a real feed is the obvious next step.
3. **18 instruments, not 26.** Only 17 FX pairs plus XAUUSD have source data
   under `C:\Trade Data`. Missing: USDCHF, NZDJPY, NZDCAD, NZDCHF, EURNZD,
   GBPNZD, AUDNZD, CADJPY, CADCHF. Consequently NZD strength rests on one
   pair and CHF on two; both are flagged `thin_coverage` in the payload and
   badged in the UI.
4. `C:\Quant` does not exist on this machine; the brief's reference to it
   could not be honoured.
5. The candle store covers 2024-01-01 onward (bounded deliberately to keep a
   full ingest at ~35s). Earlier history is available in the CSVs via
   `python -m market.ingest --start YYYY-MM-DD`.
6. D1 bars are UTC-midnight bins, not the 17:00 New York convention that
   `research/instruments.py` documents. Stated explicitly rather than
   silently assumed.
7. The Scanner and Journal tabs in the top nav are navigation placeholders;
   only Market X-Ray and Order Blocks have content.

### Dependencies required from other agents

* **Coordinator**: sign-off on the `apps/web/` ownership exception
  recorded below.
* **Antigravity** (`apps/web/` owner): the dashboard lives in its own
  `src/marketxray/` subtree and a separate `market.html` entry, so it
  should not conflict with companion-shell work. Shared-file edits needing
  review: `vite.config.ts` (added `build.rollupOptions.input` for the
  second entry), `src-tauri/capabilities/default.json` (the `market`
  window label), `src-tauri/src/lib.rs` (market window, tray item,
  commands, `--market`/`--quit`, and the scoped close handler) and
  `src-tauri/src/supervisor.rs` (the adopt-when-healthy fix). The last two
  change existing behaviour rather than only adding to it, so they deserve
  a closer look than the rest.
* **Codex**: no contract under `contracts/` was added or changed — the market
  API is additive and outside the trading-event contract.

### Ownership note

`AGENTS.md` assigns `apps/web/` to Antigravity. The user directed this
session to build the feature full-stack as lead developer, which is a
coordinator override; it is recorded here rather than treated as licence to
keep working in that lane. Nothing outside `src/marketxray/`, `market.html`,
`vite.config.ts` and the two `src-tauri/` files was modified.

### Next recommended action

Wire `MT5Provider` into `MarketXRayService` so `_feed_last_tick` is fed by
a real authenticated session. That is the only part of the feed-status
machinery never exercised against a live feed: REPLAY, CLOSED and the
no-feed path are all verified, but LIVE and STALE have only been proven by
unit test, because MT5 has no logged-in account on this machine and the
market was closed all session.

Worth doing alongside it: the per-WebSocket-frame cost is real
(`_build_frame` recomputes the X-Ray, strength and leaders for every
connected client every 10s). A short-lived cache keyed on the cursor would
make that near-free, since the underlying data only changes when the
cursor moves.

### Release-readiness review (before the push)

The complete `db49310..HEAD` diff was read before pushing: 38 files,
+7911/-5.

**Secrets and unintended files: clean.** No API key, token, password or
private key appears in any added line; the one `authoriz` hit is the
documented MT5 error string `-6 Authorization failed`. No high-entropy
literals, no email addresses, no `.env`. Nothing generated is tracked --
the SQLite state file, `artifacts/market-xray/` and
`artifacts/market-desktop/` are all confirmed ignored via
`git check-ignore`.

One stray untracked file predates this work and was deliberately left
alone: `artifacts/pocket-trial.wav`, from an earlier voice session. It is
not ignored, so a future `git add -A` would commit what may be a recording
of the user's voice. Flagged rather than silently ignored, because an
ignore rule could equally hide a sample someone intends to commit.

#### BackendSupervisor ownership and shutdown -- reviewed

Correct as it stands:

* `stop()` is **ownership-agnostic**. It POSTs `/api/v1/runtime/shutdown`
  regardless of how the backend came to exist, and `child.take()` is simply
  a no-op when the backend was adopted. That is what makes the new
  adopt-when-healthy branch safe: adopting does **not** create a backend
  that Quit can no longer stop. Confirmed empirically -- `--quit` leaves no
  listener on :8000.
* `intentional_stop` is set *before* the shutdown POST, so the supervising
  loop cannot mistake a commanded stop for a crash and auto-restart after
  it.
* The adopt branch clears the stale `Child` only after `try_wait()` has
  already reaped it, so there is no zombie and no handle leak, and it
  resets `consecutive_failures` so a later genuine crash starts from a
  clean count.
* `--quit` is byte-identical to the tray's Quit handler
  (`sup.stop(app); app.exit(0);`), so the scriptable path and the human
  path cannot diverge.
* The market window's close handler returns early only within
  `CloseRequested`; the closure handles no other event, so nothing is
  skipped for that window.

Accepted risks, recorded rather than fixed -- all of them in
`apps/web/src-tauri/`, which is **Antigravity's lane**:

1. **Adoption has no identity or version check.** `health_check_once()`
   only requires HTTP 200 from `:8000/api/v1/health`, so the shell will
   adopt *any* backend answering there -- including an orphan left by a
   previous, older build. After an upgrade the new UI would then run
   silently against old backend code. This is the strongest reason
   clean-install verification matters, and why that check is recorded as
   pending rather than assumed.
2. **A hard kill orphans the backend.** The child is not in a Windows Job
   Object with `KILL_ON_JOB_CLOSE`, so `TerminateProcess`, a crash or power
   loss leaves Python holding the port. It self-heals on the next launch
   via adoption -- which runs straight into risk 1. `--quit` is the
   cooperative path and is verified.
3. **Port 8000 is a compile-time constant** in `supervisor.rs`, while
   `chart_watcher.rs` honours `TARS_BACKEND_URL`. The inconsistency
   predates this work. Consequence introduced here: the desktop launcher's
   `-BackendPort` can only move where the *script* polls, never where the
   shell supervises. The parameter's own comment says so, but it is a knob
   that cannot do what its name suggests.
4. **Adoption can now claim a foreign backend mid-session** -- one started
   by `start-market-xray.cmd`, say -- and will shut it down on Quit.
   Consistent with the module's documented adopt-or-spawn contract, but
   previously reachable only at startup.
5. `stop()` leaves health reported as `Degraded`; there is no `Stopped`
   variant. Cosmetic, pre-existing.

### Verification still pending -- do not describe these as done

1. **Manual tray-icon click** (same item as "Known limitations" 1 above).
   Needs a human to click the tray item once and confirm the window opens.
2. **Clean-install verification.** Every check ran against this
   development tree, with its toolchain, `node_modules`, a built candle
   store and a warm data root. `TARS_1.0.0_x64-setup.exe` was produced but
   never installed on a machine lacking those. Needs: install on a clean
   Windows profile, open Market Intelligence from the installed app,
   confirm the backend starts and the dashboard paints. Risk 1 above makes
   this a real check, not a formality.

### Proposed next phase -- live connectivity, coverage, feed integrity

Not started. Recorded here for a coordinator to assign via `TASKS.md`,
per the Git workflow rule. Scope bounds carried forward from the user:
**no new trading indicators, trading execution stays disabled, existing
desktop behaviour and regression coverage preserved.**

**A. Reliable live market connectivity.** Wire `monitors/mt5_provider.py`
into `MarketXRayService` behind the existing provider seam, so
`_feed_last_tick` is fed by a real authenticated session. This needs a
logged-in MT5 account, which does not exist on this machine yet (`-6
Authorization failed`) -- clearing that is a credential/environment task,
not a code one, and it is the first blocker. The adapter must leave the
no-feed path intact so the app still runs with nothing connected, and must
not become a hard dependency: the core runtime stays free of any paid API.

**B. Strict LIVE / STALE / REPLAY separation.** The invariant to preserve
is the one already built: only a real feed can produce LIVE, and stored
candles never reach that code path. Work: exercise LIVE and STALE against
a real session during market hours (today both are proven only by unit
test); add a test that a feed whose last tick ages past the freshness
window transitions LIVE -> STALE without intervention; assert at the
service boundary that an active replay cursor can never emit LIVE even
when a live provider is attached. Carrying a provider-identity field on
every observation, so a payload states which source produced it, is the
cleanest way to make this checkable rather than merely intended.

**C. Coverage from 18 toward 26.** The ceiling is source data, not code:
nine pairs (USDCHF, NZDJPY, NZDCAD, NZDCHF, EURNZD, GBPNZD, AUDNZD,
CADJPY, CADCHF) have no CSVs under `C:\Trade Data`. Either obtain history
for them, or derive the missing legs from a live feed going forward and
accept a short history. Until then NZD rests on one pair and CHF on two,
and both must keep their `thin_coverage` flag -- closing the gap silently
would be worse than the gap itself. New instruments must not weaken the
leave-one-pair-out diagnostic that makes thin coverage visible.

**D. Prerequisite hygiene this review surfaced.** Two items in
`apps/backend/` (Claude's own lane) that A-C will touch anyway:
`market/ingest.py` hardcodes `CSV_ROOT = C:/Trade Data` and
`DEFAULT_STORE_ROOT = D:/TARS_DATA/...` instead of reusing
`research/config.py`'s `resolve_data_root()`, which already implements this
repo's `TARS_DATA_ROOT` convention -- on this machine the resolver returns
the same path, so the switch is behaviour-preserving here while making the
module portable. `scripts/start-market-desktop.ps1` hardcodes the same
store root. Risks 1-3 above are the matching items in Antigravity's lane;
risk 1 (an adoption identity check) is worth raising with that owner
before any packaged release.

## Previous handoff — Alexa-speed hot-state, Phases E-H: deep verification, provider health, benchmark harness (2026-08-21)

**Branch**: `feature/realtime-hot-state`
**Base SHA**: `17713e4` (unchanged across the whole effort)
**Final SHA**: `4eec097` (Phase E: `b636287`, Phase G: `f601f57`, Phase H: `4eec097`)

All eight phases of the Alexa-speed hot-state architecture are now
implemented and committed on this branch, not merged anywhere. See the
Phase A/B+C/D entries below for the earlier phases; this entry covers the
remainder plus a scope note on Phase F.

**Phase E — async deep verification** (`b636287`): after a fast response,
the router now schedules `assistant/deep_verification.py`'s
`run_deep_verification()` via FastAPI `BackgroundTasks` -- runs a real
re-verification vision call after the response has already gone out,
compares it against what was served (`materially_different()`: bias,
setup, action, invalidation, key levels -- never wording-only changes),
and updates `HotChartState` for the *next* request if it genuinely
differs. Caught a real integration bug via a corrected test before this
shipped: the router passes `ChartAnalysisResult.to_dict()`'s output
(which adds `speech_text`/`formatted_tars_text`) as the "served" payload
to compare against, and reconstructing a `ChartAnalysisResult` from that
dict directly would reject those two extra keys -- fixed by filtering to
known dataclass fields before reconstructing, and by changing the test to
use the same `.to_dict()` the router actually calls (a `dataclasses.
asdict()`-based version of the test had missed this entirely). No push
channel exists yet to proactively notify an already-answered user of a
correction -- same "no consumer yet" reasoning Phase C used to defer its
own broadcast wiring.

**Phase F — voice latency: mostly already true, one piece not
implementable as scoped**. Confirmed via Phase A's recon: STT
(faster-whisper) and TTS (Kokoro) are already warm singletons loaded once
at startup, not per-request, and TTS is already sentence-chunked. The one
remaining piece in the original plan -- partial-transcript prefetch (Part
12: "begin intent detection from partial transcript") -- turns out not to
be buildable as a "narrow" addition: `faster-whisper` in this codebase is
used as a **batch** transcriber, only run after `wake_engine.rs`'s
silence-based utterance finalization, not a streaming ASR that emits
partial hypotheses mid-utterance. There is no partial-transcript signal
anywhere in the current pipeline to hook into. Implementing genuine
partial-transcript prefetch would require adding real streaming STT first
-- a separate, materially larger undertaking, not a small addition to this
effort. Reported honestly rather than building something cosmetic that
looks like prefetch but has no real partial signal driving it.

**Phase G — provider health tracking** (`f601f57`):
`assistant/provider_health.py`'s `ProviderHealthTracker` computes real
success rate / P50 / P90 / P95 / last error from Phase A's `request_traces`
table -- this repo's own recorded evidence, never assumed numbers.
Exposed at `GET /api/v1/diagnostics/provider-health` (same dev-only
exposure level as the existing latency/readiness endpoints). Scope stated
honestly: this repo has exactly one vision-capable provider adapter
(`ClaudeCodeProvider` -- `build_chart_assistant_provider()` always returns
it), so there is no second real candidate to dynamically route
chart-analysis requests between yet. `healthiest()` is a real, tested
selection helper (success-rate-first, then lowest P50) usable once/where
more than one provider genuinely serves the same capability -- not wired
into `AssistantRouter`/`ChartAnalysisService`'s construction this session,
since both are built once at app startup from static config today; making
provider choice a genuine per-request decision is a separate, larger
structural change than "add health tracking off telemetry."

**Phase H — benchmark harness, and the acceptance evidence itself**
(`4eec097`): `scripts/chart_latency_benchmark.py`, a real CLI driving
warm/cold runs against a live backend with a real chart image, reporting
P50/P90/P95/max plus Part 25's quality checklist (a fast-but-fabricated
response fails the run). Uses `httpx` (already pinned) rather than adding
`requests` as a new dependency -- caught mid-session that `requests` was
only ever present in this environment via unrelated third-party packages,
not anything this repo declares.

**Actually run, not just built** -- the headline acceptance numbers for
the whole effort:

| | P50 | P90 | P95 | max | sample |
|---|---|---|---|---|---|
| **Warm path** (real backend, real chart, real HotChartState) | 0.744s | 1.511s | 1.714s | 2.140s | 20 runs, 0 errors, 0 quality failures |
| **Cold path** (formal tool cross-check) | 26.0s | 28.3s | 28.7s | 29.1s | 5 runs, 0 errors, 0 quality failures |
| **Cold path** (Phase A's original live baseline) | 24.3s | 28.3s | 28.5s | 28.7s | 7 runs |

Part 24's acceptance bar (>=18/20 warm runs within ~5s) is cleared with
the full 20/20, and every single warm run landed under 2.2s -- not a
narrow pass. A bug in the harness itself (60s client timeout, too tight
for at least one real vision call observed exceeding it) was caught and
fixed by actually running the tool against a live backend, not by
inspection.

**Files changed this entry**: `apps/backend/assistant/{deep_verification,
provider_health}.py` (new), `apps/backend/app/routers/diagnostics.py`
(new endpoint), `apps/backend/app/routers/assistant.py` (background task
wiring), `apps/backend/scripts/chart_latency_benchmark.py` (new),
`apps/backend/tests/test_{deep_verification,provider_health,
chart_latency_benchmark}.py` (new), `test_analyze_chart_fast_path.py` /
`test_diagnostics_router.py` (added cases).

**Tests run**: 13 `deep_verification` cases, 7 `provider_health` cases, 8
`chart_latency_benchmark` pure-logic cases, plus router-level integration
tests for background-task scheduling and the new diagnostics endpoint.
Full backend suite: **628 passed** on the final clean run (an earlier run
this session showed one flaky, pre-existing `test_skill_desktop_control.py`
failure -- confirmed passing 20/20 in isolation and on the next full run,
consistent with this repo's own documented `SetForegroundWindow`/UIA
flakiness history, not a regression). `ruff check` and `mypy` clean
throughout (same 7 pre-existing, untouched errors in provider adapters and
`skill_registry/db.py`).

**Known limitations, stated plainly for whoever picks this up next**:
- No WebSocket "state updated" push exists yet (Phase C deferred it, Phase
  E's corrections currently only affect the *next* request, not an
  in-flight conversation).
- Fast path only serves `Freshness.HOT` state, not `WARM` (a documented,
  deliberate scope decision from Phase D -- `WARM` needs an "as of Ns ago"
  honesty caveat that doesn't exist yet).
- Symbol/timeframe-change detection relies entirely on the vision call
  itself plus the perceptual-hash content check -- the originally
  discussed cheap OCR pre-check (`Windows.Media.Ocr`) was never built;
  the perceptual-hash approach closes the correctness gap it would have
  addressed, but OCR would let the *watcher* detect a symbol switch
  without waiting for its own vision-call cooldown to elapse.
- Provider health tracking exists but isn't wired into a live routing
  decision (Phase G, above) -- no second vision-capable provider exists to
  route between yet.
- No multi-hour resource/CPU/RAM profile of the watcher running
  continuously (Part 21) -- only interactive capture cycles were exercised
  this session.
- HASH_DIFF_THRESHOLD (14/256 bits, both Rust and Python sides) is a
  reasoned starting constant, not empirically tuned against a large
  real-world sample.

**Exact dependencies required from other agents**: same as every prior
entry in this arc -- `apps/web/src-tauri/` was touched across Phases C/D
(new files + two additive `ScreenCaptureResult`/`Cargo.toml` changes,
nothing existing modified); flagged for Antigravity/coordinator review
before any merge. `apps/web/src/types/actions.ts` also touched (additive
field only).

**Next recommended action**: a coordinator decides whether to merge this
branch (not attempted automatically, per the task's own instruction).
Before merging: install `apps/web` node_modules and do a real Tauri build
+ manual capture test in an environment with a GPU/real display (this
session's `cargo check`/`cargo test`/live captures all ran directly on the
target machine already, but a full packaged-app smoke test is still
worth doing before shipping). If pursued further: real OCR-based
symbol/timeframe detection for the watcher, WARM-tier fast responses with
an honesty caveat, the WebSocket state-update push once Phase D/E have a
real UI consumer to notify, and streaming STT as a prerequisite for any
real partial-transcript prefetch.

---

## Latest handoff — Alexa-speed hot-state, Phase D: real fast-path proof (2026-08-21)

**Branch**: `feature/realtime-hot-state`
**Base SHA**: `17713e4` (unchanged)
**Final SHA**: `f787e98`

**The headline result**: a genuine, live, end-to-end measurement, not a
projection. Simulated the watcher pushing a real WGC-captured frame to
`/api/v1/chart-watch/frame` (real Claude Code CLI vision call, 19.3s,
persisted `HotChartState`), then immediately called
`/api/v1/assistant/analyze-chart/stream` against the same window with a
fresh capture of the same chart. **Total response time: 0.537s**, correctly
served the same accurate, real analysis, correctly flagged
`timing.warm_path: true`. That's this repo's own 15-31s cold baseline
(Phase A entry below) down to well under 1s -- a ~30-50x speedup, well
inside the task's ~5s target, for the case that matters (a window the
watcher has recently seen).

**What actually shipped**: `/api/v1/assistant/analyze-chart/stream` now
tries `assistant/fast_chart_response.py`'s `try_fast_response()` first;
`None` means "nothing honest to serve fast," and the handler falls through
to the existing cold `analyze_stream()` path completely unchanged -- there
is no code path where a fast answer gets fabricated. Deliberately
conservative for this first cut: only `Freshness.HOT` state is served (not
`WARM`, which `HotChartState.usable_for()` would technically allow) --
serving `WARM` honestly needs an "as of Ns ago" caveat that
`ChartAnalysisResult`'s render methods have no hook for yet; flagged as a
follow-up, not silently dropped. The fast path is also only attempted for
the generic "Analyze this chart." goal -- any more specific question
(a different `goal_text`) always gets a fresh vision call, since a cached
generic read could easily answer the wrong question.

**A real correctness gap found and closed before this could be wired
safely**: a fast-path lookup only has `chart_window_id` at request time,
not the exact `symbol`/`timeframe` identity `usable_for()` needs (that's
only known *after* a vision call runs). Age-based freshness alone cannot
catch the Part 19/26 case this implies: the user switches symbols in the
same window, then asks again before the watcher's own vision-call cooldown
produces a fresh read for the new symbol -- a HOT-by-age row for the *old*
symbol would otherwise get served. `assistant/perceptual_hash.py` (new --
the Python half of `chart_watcher.rs`'s average-hash technique) compares
the *current* request's own capture against the stored analysis's frame;
a real symbol/timeframe switch fails this check even inside the freshness
window, ordinary price-tick movement does not. `ChartWatchService`
(Phase C) was updated to use this same check -- it previously stored a
cryptographic sha256 of the raw bytes, which would almost never match
between two real captures a few seconds apart (prices tick every second)
and made its own freshness pre-check nearly a no-op. Caught via a
dedicated regression test (`test_returns_none_when_content_has_drifted_
even_if_hot`), not by inspection.

**Rust side**: `ScreenCaptureResult` gains an additive `window_id` field
(the captured window's own HWND as a string, matching
`chart_watcher.rs`'s identity scheme exactly) so the frontend's existing
user-triggered capture can tell the backend which physical window it came
from. `capture_chart_window`/`capture_active_window` behavior is otherwise
completely unchanged; `capture_screen_region` always reports `window_id:
None` (no window concept applies to a region capture).

**A live testing pitfall worth recording**: the first attempt at this
verification used a standalone PowerShell screen-rect capture (this
session's own throwaway test harness, not any shipped code) and got a
stale/wrong result -- a different window happened to be occluding the
target's screen region at that exact moment (several windows on this
machine share identical bounds `1550x830 @(-7,-7)`, apparently all
maximized in place). That is precisely the occlusion problem Phase C's
WGC choice exists to solve, live-demonstrated by my own flawed ad-hoc
tooling, not by the production code -- `capture_wgc.rs`'s real capture
path was unaffected (already proven immune to this in the Phase C entry
below) and was used instead for the corrected, honest re-verification
above.

**Files changed**: `apps/backend/assistant/fast_chart_response.py` (new),
`perceptual_hash.py` (new), `chart_watch.py` (hash method changed),
`hot_chart_state.py` (docstring only), `apps/backend/app/{deps,routers/
assistant}.py`, `apps/backend/tests/test_{fast_chart_response,
perceptual_hash,analyze_chart_fast_path}.py` (new),
`test_chart_watch.py` (updated for the hash change + new regression case),
`apps/web/src-tauri/src/lib.rs` (additive field only),
`apps/web/src/types/actions.ts` (additive field only).

**Tests run**: 7 new `fast_chart_response` cases, 5 `perceptual_hash`
cases, 3 router-level integration tests (provider genuinely never called
via `monkeypatch` assertion on the fast path; cold path confirmed for
no-`window_id` and custom-goal cases), `chart_watch.py`'s tests updated
and passing with a new content-drift regression. Full backend suite --
**597 passed**, `ruff check` clean, `mypy` clean (same 7 pre-existing
errors, none new). `cargo check`/`cargo clippy --no-deps` clean (same one
pre-existing, untouched warning). Live verification as described above,
against the real backend, real Claude CLI, and a real WGC-captured chart
-- not a mock or a projection.

**Known limitations**:
- WARM-tier fast responses are not served yet (see above) -- narrows how
  often the fast path fires today; a real, deliberate scope decision, not
  an oversight.
- The fast path answers `capital_note`-less generic analysis only; a
  capital/profit sub-question riding on "Analyze this chart." would still
  need `TradeCalculationEngine`'s deterministic split (`chart_analysis.py`
  already does this on the cold path) -- not exercised on the fast path
  since `try_fast_response` only fires for the exact default goal text and
  a capital question changes the goal text, which routes it to cold
  automatically. Not a gap, just worth naming why it's safe.
- No WebSocket "state updated" push yet (still true from the Phase C
  entry) -- the fast path is pull-only (the user has to ask); nothing
  proactively tells the UI a fresher read exists.

**Exact dependencies required from other agents**: same as Phase C --
`apps/web/src-tauri/src/lib.rs` touched again (additive field only), no
existing behavior changed. Flagging for review before any merge.

**Next recommended action**: Phase E (async deep verification -- after a
fast response, kick the same refresh path in the background and diff for
material differences before ever pushing a follow-up correction to the
user).

---

## Latest handoff — Alexa-speed hot-state, Phases B+C: HotChartState + non-intrusive BackgroundChartWatcher (2026-08-21)

**Branch**: `feature/realtime-hot-state`
**Worktree**: `C:\TARS-worktrees\realtime-hot-state`
**Base SHA**: `17713e4` (unchanged from the Phase A entry below)
**Final SHA**: `d4ce217` (Phase B: `28e2cef`, Phase C: `d4ce217`)

**Live baseline run (between Phase A and Phase B)**: 7 valid real
`analyze-chart` requests against a real XAUUSD 15m chart in the actual
native TradingView desktop app (not mocked), driven by a standalone
harness against the Phase A-instrumented backend on an isolated port/DB.
Total time P50 24.3s / P90 28.3s / P95 28.5s / max 28.7s. First-meaningful-
text (server `first_token_ms`) P50 5.3s but P90/P95 ~23.2-23.4s -- **2 of 7
runs got zero visible/speakable text until the entire response arrived as
one final chunk**, not the incremental stream the other 5 runs showed.
This is a real Claude CLI streaming-behavior variance, not something this
backend controls, and is a genuine risk to the "first TTS audio <=5s"
target that Phase F/H should measure again with a larger sample. Quality:
6/7 correctly read the real chart (accurate price levels, matched the
image), but **0/7 returned the strict JSON schema** the chart-analysis
system prompt demands -- all fell back to the existing, tested unstructured
markdown path. This matters for Phase D: the deterministic composer needs
parsed fields, so before relying on them, either tighten the prompt or make
the composer degrade honestly when `structured=False`. One run (excluded
from the stats above) accidentally captured this very Claude Code/IDE
session instead of the chart -- a live, unplanned demonstration of exactly
the window-occlusion problem that motivated Phase C's capture-architecture
decision below (Claude correctly refused to analyze it as a chart, which
is the correct/desired truthfulness behavior).

**Phase B — HotChartState** (`28e2cef`): new
`assistant/hot_chart_state.py` (`HotChartState`/`ChartIdentity`/
`Freshness` -- HOT/WARM/STALE/MISSING with documented, timeframe-adaptive
thresholds) and `assistant/hot_chart_state_store.py`, reusing
`ChartAnalysisResult` as the analysis payload rather than duplicating its
fields. New migration `0006_hot_chart_state.sql` (composite PK on
`chart_window_id, symbol, timeframe`, with `''` rather than SQL `NULL` for
unknown symbol/timeframe -- SQLite's `PRIMARY KEY` does not imply
`NOT NULL` for non-INTEGER composite keys, and `NULL != NULL` for
uniqueness, which would have silently allowed duplicate "unknown identity"
rows). Purely additive: nothing in the live path used it yet as of this
commit.

**Phase C — BackgroundChartWatcher** (`d4ce217`): the substantial piece.
Per explicit direction, background capture uses **Windows.Graphics.Capture
(WGC)**, not the existing `capture_chart_window` hide/BitBlt/focus-steal
path (unusable for a silent loop -- it steals OS input focus on every
restore) and not DXGI Desktop Duplication (still captures the composited
desktop, so it wouldn't actually solve the occlusion problem WGC's
per-window mode does). New Rust modules:

- `capture_wgc.rs` -- `WgcCapture`: D3D11 device + WGC capture session
  bound to a target hwnd, polled via `try_capture_frame()`. **Verified
  against the real, live TradingView window this session**, not just
  compiled: captured a genuine 1920x1020 frame, decoded it back to PNG and
  visually inspected it (right side up, correct colors, the actual chart)
  while VS Code was in front on another window -- proving the "no
  occlusion problem" claim directly, not just asserting it. Caught two
  real bugs only a live run could catch: (1) `TryGetNextFrame`'s "no frame
  yet" case surfaces as `Err(HRESULT S_OK)` in this WinRT binding, not
  `Ok(None)` -- the first version treated every `Err` as fatal, which
  would have made the watcher never produce a frame against a real
  session; (2) D3D11 texture memory is top-down but the reused
  `create_bmp_bytes` (lib.rs) expects bottom-up rows (matching its other
  caller, `GetDIBits` with positive `biHeight`) -- unflipped, every
  background-watcher frame would have shipped upside down to Claude.
- `chart_watcher.rs` -- the polling/diff/trigger loop on its own
  background OS thread (same independence-from-the-webview pattern as
  `wake_engine.rs`). Window discovery is **not** foreground-biased (unlike
  the existing `get_target_chart_window_hwnd`, deliberately, since the
  watcher must keep tracking the chart while the user works elsewhere) --
  matches by process name or title containing "tradingview"; confirmed via
  the live baseline that the real app's process name is "TradingView" but
  its *title* is the ticker/price (not "TradingView"), so both fields
  must be checked, and an earlier test-only helper that checked title only
  was caught failing against the live window and fixed. Cheap 16x16
  average-hash frame diffing (hand-rolled, no image crate) gates whether
  to push a frame to the backend; separate cooldown floors on the Rust
  side (anti-spam, 5s) and Python side (anti-cost, per-window, default
  20s) are deliberately distinct concerns. Pauses on idle/lock
  (`GetLastInputInfo`) and when no chart window is found.
- `assistant/chart_watch.py` -- `ChartWatchService`, the real vision-call
  policy: per-window cooldown plus a freshness pre-check against
  `HotChartStateStore.get_latest_for_window()` so a still-HOT window never
  re-triggers a vision call just because Rust detected some pixel change.
  Reuses `ChartAnalysisService.analyze()` verbatim -- same system prompt,
  same epistemic discipline, same disclaimer -- never a second vision-call
  path.
- `app/routers/chart_watch.py` -- `POST /api/v1/chart-watch/frame`, the
  Rust watcher's receiving endpoint (not reachable from the
  frontend/webview, matching `wake_engine.rs`'s own backend-direct
  pattern for `/api/v1/voice/transcribe`).

**Deliberately not done in Phase C**: the state-update WebSocket broadcast
Part 3 mentions is not wired -- there is no consumer until Phase D's fast
path exists to react to it, and building unused pub/sub plumbing now would
be speculative infrastructure. `HotChartState` persistence itself is fully
wired and tested; a future session adds the broadcast once Phase D needs
it.

**Files changed** (Phase B+C combined): `apps/backend/assistant/
hot_chart_state.py`, `hot_chart_state_store.py`, `chart_watch.py` (new),
`apps/backend/app/routers/chart_watch.py` (new), `apps/backend/app/
{deps,main}.py`, `apps/backend/storage/migrations/0006_hot_chart_state.sql`,
`apps/backend/tests/test_hot_chart_state{,_store}.py`,
`test_chart_watch{,_router}.py`, `apps/web/src-tauri/src/{capture_wgc,
chart_watcher}.rs` (new), `apps/web/src-tauri/src/lib.rs` (module
registration + startup wiring only -- `capture_chart_window`/
`capture_active_window`/`get_target_chart_window_hwnd` untouched),
`apps/web/src-tauri/Cargo.toml` (additive `windows` crate dependency +
two new `windows-sys` features for idle detection).

**Tests run**: Rust -- `cargo check`, `cargo clippy --no-deps` (clean
except one pre-existing, untouched warning in `lib.rs`), `cargo test --lib`
(6 pure-logic tests passing; the live-hardware capture test is
`#[ignore]`d by default and was run explicitly this session, see above --
it needs a real TradingView window open and is not part of normal CI).
Backend -- full suite **581 passed**, `ruff check` clean, `mypy` clean
(same 7 pre-existing errors as Phase A, none new, none in touched files).

**Known limitations**:
- Chart-window discovery is hardcoded to matching "tradingview" in
  process name or title -- not configurable yet if a different charting
  app needs support later.
- The average-hash diff threshold (`HASH_DIFF_THRESHOLD = 14` out of 256
  bits) is a reasoned starting constant, not empirically tuned against a
  large real-world sample of "meaningful vs. cosmetic" chart changes.
- Symbol/timeframe-change detection relies entirely on the vision call
  itself (via `ChartIdentity`) -- the originally discussed cheap local OCR
  of the ticker label (`Windows.Media.Ocr`) to detect a symbol switch
  *before* spending a vision call was not built this session; flagged as a
  Phase C follow-up, not silently dropped.
- No physical, multi-hour resource/CPU/RAM measurement of the watcher
  running continuously yet (Part 21) -- only a few real capture cycles
  were exercised interactively this session.
- The Rust `windows` crate direct dependency sits alongside two already-
  transitively-resolved `windows` crate versions (0.61.3, 0.62.2) pulled
  in by `tauri`'s own dependency tree -- confirmed this compiles and links
  correctly (both `cargo check` and the live capture test succeeded), just
  flagging the duplication for awareness, not a defect.

**Exact dependencies required from other agents**: this session again
touched `apps/web/src-tauri/` (Antigravity's normal lane per `AGENTS.md`)
-- two new files (`capture_wgc.rs`, `chart_watcher.rs`) plus additive-only
changes to `lib.rs`/`Cargo.toml`. No existing Rust command, struct, or
behavior was modified. Flagging for review before any merge, per the same
rationale as the Phase A entry below.

**Next recommended action**: Phase D (wire a real `FAST_CHART_ANALYSIS`
path into the actual trigger -- today's `IntelligenceRouter.CHART_ANALYSIS`
branch is still a stub that just says "use the HUD trigger"; the
deterministic composer must handle `structured=False` gracefully given the
0/7 JSON-compliance finding above).

---

## Latest handoff — Alexa-speed hot-state, Phase A: latency instrumentation (2026-08-20)

**Branch**: `feature/realtime-hot-state`
**Worktree**: `C:\TARS-worktrees\realtime-hot-state` (dedicated, per the
task's own isolation requirement — do not confuse with the main
`c:\TARS-Overnight-Integration` checkout, which stays on
`feature/trading-intelligence-architecture`)
**Base SHA**: `17713e4` (tip of `feature/trading-intelligence-architecture`
at session start — includes the Claude/Codex provider work, trading-
intelligence fixes, and skill system)
**Final SHA**: `aa21d8a`

**Ownership note**: this is a multi-phase, cross-stack feature ("TARS
should begin a meaningful chart response in ~5s instead of 15-25s")
explicitly directed at one agent building the full stack in an isolated
worktree, specifically so it doesn't collide with other agents' concurrent
sessions on the shared repo. `apps/web/` (including `src-tauri/`) is
normally Antigravity's lane per `AGENTS.md` — later phases of this same
effort (background chart watcher, native capture) will touch
`apps/web/src-tauri/*.rs` and `apps/web/src/*`; each such change will be
called out explicitly in this file for review before any merge. This
Phase A session touched one frontend file
(`apps/web/src/runtime/ChartAnalysisClient.ts`, one additive line — see
below); everything else is `apps/backend/`. `contracts/*.schema.json` was
not touched.

**Work completed** — Phase A only (baseline latency instrumentation; the
task requires measuring a real baseline before changing behavior, not
optimizing on anecdote): the codebase already had two pieces of latency
infrastructure that were built but never actually wired up —
`ProviderDiagnostics` (`assistant/provider.py`, added by an earlier
session) was computed on every provider call but discarded immediately
(`assistant/router.py`'s `_call_provider` built it then only used
`reply.text`/`reply.provider`), and `app/latency.py`'s `LatencyTracker` was
exercised only by its own unit tests, never called from a live request
path. Phase A closes that gap rather than inventing a third timing
mechanism:

1. **`storage/migrations/0005_request_traces.sql`** — new
   `request_traces` table: one row per chart-analysis or text-chat
   request, with `capture_ms`/`provider_start_ms`/`first_token_ms`/
   `provider_latency_ms`/`total_ms`/`error`, indexed on `(kind,
   started_at)`.
2. **`app/latency_store.py`** (new) — `LatencyTraceStore`, the same
   `*Service`-wrapping-the-shared-`aiosqlite`-connection pattern every
   other service in this backend uses (`EventService`,
   `ConversationStore`, etc.). `record()` writes a trace;
   `percentiles(kind)` computes P50/P90/P95/max/error-count in Python
   (linear-interpolation percentile, since SQLite has no
   `PERCENTILE_CONT`).
3. **`assistant/chart_analysis.py`** — `ChartAnalysisService` takes an
   optional `trace_store` (default `None`, so every existing caller/test
   keeps working unchanged). `analyze_stream()` now records a trace on
   every exit path: success (provider name, `claude_start_ms` as
   `provider_start_ms`, `first_token_ms`, total), image-decode failure,
   and any provider exception — never only the happy path, since the
   whole point is real baseline numbers including failures.
4. **`assistant/router.py`** — `AssistantRouter` takes the same optional
   `trace_store`; `_call_provider()` now persists `reply.diagnostics`
   (finally using the field that was always being computed) on both
   success and `AssistantProviderError` failure.
5. **`app/routers/diagnostics.py`** (new) —
   `GET /api/v1/diagnostics/latency?kind=chart_analysis&limit=200`,
   same exposure level as the existing `/api/v1/runtime/readiness`
   (developer-facing, not called by the HUD/voice UI, no secrets).
6. **Capture-stage timing threaded end to end**: the frontend already
   computed `captureMs` (hide/DWM-wait/BitBlt/restore) locally for its own
   console `[PERF][chart]` marks but never sent it to the backend —
   `ChartAnalysisClient.ts` now includes `capture_ms` in the POST body
   (one additive line), `app/routers/assistant.py`'s
   `analyze_chart_stream` reads it, and it flows into the trace row. This
   is the one frontend file this session touched.
7. Wiring: `app/main.py` constructs one `LatencyTraceStore` off the
   shared DB connection at startup and passes it into both
   `ChartAnalysisService` and (via `app/deps.py`'s
   `get_assistant_router`) `AssistantRouter`.

**Files changed**: `apps/backend/storage/migrations/0005_request_traces.sql`
(new), `apps/backend/app/latency_store.py` (new),
`apps/backend/app/routers/diagnostics.py` (new),
`apps/backend/app/deps.py`, `apps/backend/app/main.py`,
`apps/backend/app/routers/assistant.py`,
`apps/backend/assistant/chart_analysis.py`,
`apps/backend/assistant/router.py`,
`apps/backend/tests/test_chart_analysis.py` (added streaming-trace cases),
`apps/backend/tests/test_latency_store.py` (new),
`apps/backend/tests/test_latency_trace_wiring.py` (new),
`apps/backend/tests/test_diagnostics_router.py` (new),
`apps/web/src/runtime/ChartAnalysisClient.ts` (one additive line).

**Tests run**: full `apps/backend` suite — **553 passed**, 0 failed, 0
regressions. `ruff check` on every touched/created file — clean (3
findings auto-fixed: unnecessary quoted forward-refs now that
`from __future__ import annotations` makes them unneeded, one unsorted
import block). `mypy --config-file pyproject.toml app assistant` —
clean, same 7 pre-existing errors as before this session (in
`assistant/providers/{gemini,codex,claude_code}.py` and
`skill_registry/db.py` — none in any file this session touched).
Frontend change **not** type-checked with `tsc` — `apps/web/node_modules`
is not installed in this worktree (consistent with prior sessions'
handoffs noting the same gap); the change itself is a single additive
object-literal field with no type surface change, low risk, but flagging
per this repo's honesty convention rather than claiming a check that
didn't run.

**Known limitations / deliberately deferred**:

- **No live P50/P90/P95 numbers from a real running app yet.** This
  session proves the plumbing (trace rows are written correctly on every
  exit path, percentiles compute correctly — see the new test files) but
  has not run the actual `claude` CLI against a real captured chart N
  times to produce real baseline numbers. That requires either the user's
  own TradingView session or a live backend + Tauri app running
  end-to-end, which this session didn't have. The commit-message numbers
  already in this repo's history (`8ce9ea1`, `9159069`: 10-31s, model
  inference dominates) remain the best evidence until a live run happens
  — Phase H's benchmark harness is the right place to automate that,
  though a spot real-world check earlier would sharpen Phase B-D's exact
  threshold choices.
- **`respond_stream()` (both `ClaudeCodeProvider` and any future
  provider) still doesn't emit `ProviderDiagnostics`** — only the
  non-streaming `respond()` does. The chart-analysis streaming path's
  trace instead uses its own `claude_start_ms`/`first_token_ms`/
  `complete_ms` marks (already existed, just now persisted), which cover
  the same ground for that path specifically. If a future phase wants
  richer diagnostics (exit code, request_id, model name) on the streaming
  path too, `respond_stream`'s `complete` event would need an additive
  `diagnostics` field — not done here to keep this phase additive-only
  and scoped to persistence, not provider-adapter changes.
- **General-chat streaming (`handle_text_stream`) is not traced** — only
  `handle_text`'s non-streaming `_call_provider()` path is. The streaming
  chat path's provider branch doesn't produce a `ProviderDiagnostics`
  either (see above), so there was nothing to persist yet; adding a
  timing-only trace there without diagnostics felt like it would produce
  a real-but-thin row that invites over-reading. Left for whoever wires
  streaming diagnostics.
- **Dev-endpoint exposure, not access-controlled** — matches
  `/api/v1/runtime/readiness`'s existing exposure level (this app binds
  to `127.0.0.1` by default per `ARCHITECTURE.md`); no new gating pattern
  invented.

**Exact dependencies required from other agents**: none blocking. Later
phases of this same effort (B: HotChartState model/persistence; C:
BackgroundChartWatcher, which needs new Rust capture code in
`apps/web/src-tauri/`) will cross into Antigravity's normal ownership —
flagged in advance here per `AGENTS.md`'s "flag it in your handoff instead"
guidance, not attempted without disclosure.

**Next recommended action**: Phase B (HotChartState domain model +
`0006_hot_chart_state.sql`, purely additive, no behavior change) on the
same branch/worktree. Full plan (all 8 phases) is tracked outside this
repo in the originating conversation; ping the coordinator if a written
copy in `docs/coordination/` would help future sessions pick this up
independently.

---

## Latest handoff — Wave 2B: Windows context + desktop-control skills (2026-08-17)

**Branch**: `feature/wave2b-context-windows`
**Base SHA**: `877dde0bd5f93817b78566eb4c8e0f9a58106c4f` (`integration/wave2`,
post Wave 2A/M2A merge, ADR-022)
**Final SHA**: `644fbe4` (implementation+tests commit; this handoff and
`docs/coordination/wave2/m2b/claude.done.json` land in a follow-up
docs-only commit on the same branch — same convention as prior entries in
this file).

This is a **continuation of the Wave 2A architecture**, not a new one —
`User/Voice/HUD/Context -> ActionRequest -> Action Runtime -> Permission
Engine -> Skill Registry -> Windows skill -> ActionResult + Audit` is
unchanged. Nothing in this session bypasses the Action Runtime or
Permission Engine; there is no independent agent loop (Codex still owns
orchestration/control runtime).

**Work completed**:

1. **Rich active-window context** — `app.action_contracts.ActiveWindowContext`
   (and its mirror, `contracts/action-request.schema.json`'s
   `$defs/active_window_context`) gained four new **optional** fields:
   `window_state` (normal/minimized/maximized/unknown, via
   `win32gui.IsIconic`/`IsZoomed`), `monitor` (geometry + `is_primary`, via
   `win32api.MonitorFromWindow`/`GetMonitorInfo` — geometry only, no pixel
   data), `focused_control` (identity metadata only — control type/name/
   automation id/class name, **no value/content**, and `name` is omitted
   entirely when the focused control `IsPassword`), and `source`
   (`win32`/`ui_automation`, which capture path produced the context).
   This is purely additive — `additionalProperties: false` blocks were
   extended, not loosened, and `actions/requests.py`'s
   `_ALLOWED_CONTEXT_FIELDS`/`_ALLOWED_MONITOR_FIELDS`/
   `_ALLOWED_FOCUSED_CONTROL_FIELDS` allowlists moved in lockstep with the
   schema and Pydantic model, per the existing "these three things drift
   together or not at all" pattern the M2A explore report flagged.
   **Deliberately excluded**: `selected_text` and clipboard content are
   **not** fields on `ActiveWindowContext` — see point 4 below for why.
2. **Windows UI Automation layer** — new `skills/_desktop_automation.py`.
   Real UIA via the `uiautomation` package (comtypes-based COM interop,
   not a mock/simulation layer): `resolve_window()` (reuses
   `windows_app._find_window` for executable/title matching, or the
   foreground window for "current"), `control_from_hwnd()`
   (`auto.ControlFromHandle`), `walk_actionable_controls()` (bounded
   BFS — depth-capped, count-capped, and separately visit-capped so a
   pathological tree like File Explorer with thousands of items can't
   cause unbounded work), `serialize_control()` (type/name/automation-id/
   class-name/bounds/enabled/password-flag — `IsPassword` controls have
   `name` redacted to `null`), and the actual pattern-driving functions:
   `do_focus`/`do_invoke`/`do_type`/`do_select`/`do_scroll`, each calling
   a real UIA pattern (`InvokePattern`/`TogglePattern`/
   `LegacyIAccessiblePattern` for invoke; `ValuePattern`/
   `LegacyIAccessiblePattern` for type; `SelectionItemPattern`/
   `LegacyIAccessiblePattern` for select; `ScrollPattern` for scroll) —
   **no blind-coordinate clicking, no keystroke simulation**, per the spec.
   `read_selected_text()` uses `TextPattern.GetSelection()` and refuses
   (raises) on a password control rather than reading it.
   `read_clipboard_text()` uses `win32clipboard`'s `CF_UNICODETEXT` format
   only, returning `None` (not an error) when the clipboard holds no text.
3. **Control identity / `ControlHandleCache`** — UIA `Control` objects
   aren't JSON-serializable or independently re-derivable by pointer, so
   `list_controls`/`inspect_current_window` hand back an opaque
   `control_id` (a random `uuid4().hex` token, not a pointer/runtime-id
   encoding) that resolves through a small in-process cache: TTL 180s,
   capped at 400 entries (oldest evicted first on overflow). This is
   short-lived actionability state, not indefinite raw-UI-tree retention —
   satisfies the "do not retain raw UI trees indefinitely" requirement
   while still letting a follow-up `focus_control`/`invoke_control`/etc.
   target the exact element it was just shown. An unknown/expired
   `control_id` fails loudly (`SkillExecutionError` -> a real `FAILED`
   `ActionResult`, "call list_controls again"), never silently no-ops.
4. **`skills/desktop_control.py` (`DesktopControlSkill`)** — 9 actions:
   `inspect_current_window`, `list_controls`, `focus_control`,
   `invoke_control`, `type_into_control`, `select_control`,
   `scroll_control`, `read_selected_text`, `read_clipboard`.
   `inspect_current_window` **is** the "context snapshot" primitive from
   the spec (active app, window title, up to `max_controls` visible
   actionable controls [default 20, hard-capped 50], best-effort selected
   text, and clipboard text **only** when `include_clipboard=true` is
   explicitly passed) — bounded, ephemeral (returned in `ActionResult.data`,
   not cached server-side), and clipboard/selected-text are opt-in per
   call rather than silently attached, matching the spec's "clipboard
   context through an explicit read action" and "do not silently scrape...
   protected desktop content" requirements. `type_into_control` refuses
   `mode="append"` on a password field (reading its current value to
   concatenate would be exactly the scraping this is meant to prevent);
   `mode="replace"` is allowed (the user directing the assistant to type
   into a field they're looking at is a normal, one-directional action,
   not a read).
5. **Risk classification** — wired into `actions/permissions.py`'s
   `_KNOWN_ACTION_POLICY["desktop_control"]`: `inspect_current_window`/
   `list_controls`/`read_selected_text`/`read_clipboard` = `READ_ONLY`;
   `focus_control`/`scroll_control` = `LOW_RISK`; `invoke_control`/
   `type_into_control`/`select_control` = `CONFIRM_REQUIRED`. This is a
   **required** explicit policy, not a convenience one — the permission
   engine's generic verb-name fallback (`_STATE_CHANGING_ACTION` regex:
   run/execute/write/create/update/install/uninstall/move/rename/copy/
   send/post/upload/download) does **not** match `invoke_control`,
   `type_into_control`, or `select_control`, so without this entry those
   three would default to `READ_ONLY` — exactly the "typing/changing
   application state must NOT be READ_ONLY" failure mode the spec warned
   about. Verified directly (see Tests).
6. **`skills/registry.py`** — `desktop_control` added to the eager
   `SKILLS` dict (stateless aside from its own bounded control cache, same
   category as the other four DB-independent skills).

**Real Windows verification** (Windows 11, this sandbox — not mocked):
launched a real `notepad.exe` against a uniquely-named temp file (modern
Windows 11 Notepad is a single shared-process, multi-tab MSIX app — a
plain `Popen(...).kill()` on the launcher stub does **not** close the tab,
since the stub exits immediately after activating the shared host; test
teardown closes precisely that test's own tab via its real "Close Tab" UIA
button, never the shared process, so it can't disturb the user's other,
unrelated open Notepad tabs — confirmed by re-diffing the window list
before/after). Against that real window: `list_controls` found its actual
`DocumentControl` ("Text editor" / `RichEditD2DPT`) and its real "Add New
Tab" button (`AutomationId="AddButton"`); `type_into_control` genuinely
set the editor's text via `ValuePattern.SetValue` (confirmed via
`vp.Value` readback during exploration); `invoke_control` genuinely
clicked "Add New Tab" via `InvokePattern.Invoke()` and a real new tab
appeared (then cleaned up); `select_control` genuinely selected a real
`TabItemControl` via `SelectionItemPattern.Select()`; `scroll_control`
genuinely scrolled the editor via `ScrollPattern.Scroll()` after filling
it with 500 lines (confirmed `VerticallyScrollable=True` first);
`read_selected_text`/`read_clipboard` returned real, honest empty/present
state, not fabricated data. **A real bug was caught this way**: an early
draft called `win32gui.GetWindowThreadProcessId`, which doesn't exist —
that API is `win32process.GetWindowThreadProcessId` — and this only
surfaced because the exploration script crashed against a real window;
a mock would not have caught it. Fixed in
`skills/_desktop_automation.py`.

**Files changed**: `apps/backend/skills/_desktop_automation.py` (new),
`apps/backend/skills/desktop_control.py` (new),
`apps/backend/tests/test_skill_desktop_control.py` (new, 20 cases),
`apps/backend/skills/registry.py`, `apps/backend/actions/permissions.py`,
`apps/backend/actions/requests.py`, `apps/backend/app/action_contracts.py`,
`apps/backend/tests/test_skill_registry.py` (updated expected skill set),
`apps/backend/requirements.txt` (additive: `comtypes==1.4.16`,
`uiautomation==2.0.29`), `contracts/action-request.schema.json` (additive
schema extension — see point 1 above; this is normally
read-only-without-coordinator-sign-off per `AGENTS.md`, flagging
explicitly since the Wave 2B task itself directed this extension).

**Tests run** (from `apps/backend/`):

- `python -m pytest tests/test_skill_desktop_control.py -v` — **20
  passed** (real Notepad interaction, see above).
- Full suite, `python -m pytest -q` — **270 passed** on one run; a second
  full run showed **269 passed, 1 failed**
  (`test_skill_windows_app.py::test_execute_focus_finds_and_focuses_real_window`,
  `SetForegroundWindow` timing out — this is the **same pre-existing,
  environment-dependent Windows foreground-lock flake** Wave 2A's own
  `claude.done.json` already flagged for that exact test; nothing in this
  session touches `windows_app.py`, and the failure is intermittent based
  on which process last had OS input focus, not a regression).
- `python -m ruff check skills/ actions/ app/action_contracts.py
  tests/test_skill_desktop_control.py tests/test_skill_registry.py` —
  **clean** (two real `B023` loop-variable-capture findings caught and
  fixed with the standard default-argument binding pattern, not suppressed).
- `python -m mypy --config-file pyproject.toml skills` — **clean, 10
  source files** (one real finding fixed: a branched `Control | None` vs
  `Control` assignment in `_execute_read_selected_text`, restructured with
  an explicit `Control | None` local and a None-check before use).

**Known gaps**:

- **Rust/native-shell side is untouched by design.** `apps/web/src-tauri/`
  is Antigravity's ownership per `AGENTS.md`; `get_active_window_context()`
  there still only captures executable/title/bounds. The new
  `window_state`/`monitor`/`focused_control`/`source` schema fields are
  additive and safe to send from Rust once wired, but nothing currently
  populates them on the passive per-`ActionRequest` `active_context` path
  — only the backend's own `inspect_current_window` action (which queries
  live Windows state directly, independent of whatever the HUD attaches)
  populates them today. Wiring the Tauri side is a natural Antigravity
  follow-up, not attempted here (would cross ownership boundaries).
- **`invoke_control`'s InvokePattern/TogglePattern/LegacyIAccessiblePattern
  fallback chain does not include a coordinate-click fallback**, by design
  (spec: "do not use blind coordinates when a semantic UI Automation
  target exists") — a control that genuinely exposes none of those three
  patterns will fail honestly (`SkillExecutionError`) rather than falling
  back to a click simulation. Not exercised against such a control in this
  session (Notepad's controls all support at least one).
- **`type_into_control` has no keystroke-simulation fallback** — only
  `ValuePattern`/`LegacyIAccessiblePattern.SetValue`. A control that
  exposes neither (some custom-drawn/game-engine UI, rare in normal
  Windows apps) will fail honestly rather than simulating keypresses.
  Scope was intentionally kept to real UIA patterns, per the spec's
  semantic-target requirement.
- **`select_control`'s `LegacyIAccessiblePattern` fallback path
  (`SELFLAG_TAKESELECTION | SELFLAG_TAKEFOCUS`) was not exercised in this
  session** — Notepad's tabs all support `SelectionItemPattern` directly,
  so the fallback branch is implemented per the pattern's documented
  contract but not independently verified against a control that lacks
  `SelectionItemPattern`.
- **File Explorer and Settings were not verified this session** — Notepad
  covered enumeration/focus/invoke/type/select/scroll/read-text/
  read-clipboard end-to-end; File Explorer/Settings were reviewed as
  targets during design (both expose standard UIA trees) but not
  separately exercised. Recommend a follow-up targeted check if
  File-Explorer-specific quirks (e.g. its list-view virtualization) matter
  for a near-term use case.
- **No multi-step workflow orchestration was built**, intentionally — the
  spec is explicit that Codex owns orchestration/control runtime and this
  session must not create an independent agent loop. The primitives
  (`inspect_current_window`/`list_controls` for inspect+select-target,
  `focus_control` for focus, `invoke_control`/`type_into_control`/
  `select_control`/`scroll_control` for act, and re-calling
  `inspect_current_window`/`list_controls` for verify) are each
  independently dispatchable through the Action Runtime today; composing
  them into a "focus -> inspect -> select -> act -> verify" sequence is
  the orchestrator's job, not this skill's.
- **`SetForegroundWindow` foreground-lock flakiness** (see Tests) affects
  `focus_control` the same way it affects `windows_app.focus` — both call
  into UIA/win32 focus APIs that are subject to the same OS-level
  foreground-lock timeout under some process-focus histories. This is a
  pre-existing Windows OS behavior, not specific to this session's code.

**Exact dependencies required from other agents**:

- **Antigravity** (`apps/web/src-tauri/`): the schema now supports
  `window_state`/`monitor`/`focused_control`/`source` on `active_context`
  — populating them from the native shell (extending
  `get_active_window_context()` in `apps/web/src-tauri/src/lib.rs`) is
  optional/additive, not required, since the backend's own
  `inspect_current_window` action already provides this data on demand
  without depending on the HUD to supply it.
- **Codex** (`apps/backend/actions/` / orchestration): the five action
  primitives listed under "no multi-step workflow orchestration" above are
  ready to compose into a focus->inspect->select->act->verify sequence
  whenever Codex's control runtime needs one; each already goes through
  the existing Action Runtime/Permission Engine/confirmation-token flow
  unchanged.
- No blocking dependency for either — this session's additions are usable
  standalone via `POST /api/v1/actions` today.

**Next recommended action**: a coordinator decides whether this needs a
schema-version bump (currently left at `1.0.0` since the change is
strictly additive/backward-compatible — no existing consumer breaks) or
just an ADR note recording the additive extension, same treatment as
other additive contract changes in this repo's history. Antigravity can
independently pick up wiring `monitor`/`window_state`/`focused_control`
into the Tauri side whenever convenient. A follow-up session could verify
File Explorer/Settings specifically and exercise the
`LegacyIAccessiblePattern` fallback paths against a control that lacks the
primary pattern.

---

## Latest handoff — Wave 2A M2A Phase 2: skills execution layer (2026-08-17)

**Branch**: `feature/wave2-core-skills`
**Worktree**: `C:\TARS-Wave2-Claude` (do not confuse with the V1 worktrees above)
**Base SHA**: `20f2353e100a37d93621a018e72951038a50585c` (`integration/wave2`)
**Final SHA**: `f900293454afae300a64e177a142e6f86f719917` (implementation+tests
commit; this handoff/`docs/coordination/wave2/m2a/claude.done.json` are
committed in a follow-up docs-only commit on the same branch — see
`git log feature/wave2-core-skills` for its trailing SHA, same convention as
prior entries in this file: *Final SHA* names the last substantive code/test
commit, not the handoff-writing commit).

This is a **new milestone** (Wave 2A / M2A), not a continuation of the V1
backend work below — read
[`docs/coordination/wave2/M2A_SPEC.md`](../wave2/M2A_SPEC.md) and
[`M2A_INTERFACES.md`](../wave2/M2A_INTERFACES.md) first. `apps/backend/app/`,
`apps/backend/{events,voice,assistant,memory}/`, and all V1 tests are
**unmodified** except `apps/backend/requirements.txt` (one additive line,
`pywin32==312`).

**Work completed** — implemented `apps/backend/skills/`, the real Windows
skill-execution layer Codex's action runtime (`apps/backend/actions/`, not
yet built) will dispatch validated `ActionRequest`s into:

1. **`skills/windows_app.py` (`WindowsAppSkill`)** — `launch`
   (`subprocess.Popen` with an argv list, never `shell=True`; bare names
   resolved via `shutil.which`, absolute targets must be an existing
   `.exe` with no `..` segments), `focus` (real `pywin32`
   `win32gui`/`win32process`/`win32api` — enumerates visible windows,
   matches by executable basename or window-title substring, calls
   `ShowWindow`/`SetForegroundWindow`), `list_running` (read-only: exe name
   + window title only, no content). `pywin32==312` was already present
   and genuinely importable in this sandbox (`import win32gui,
   win32process, win32con, win32api` succeeds); added it to
   `requirements.txt` since it wasn't previously pinned there.
2. **`skills/filesystem.py` (`FilesystemSkill`)** — `list`/`search`
   (read-only, boundary-checked: `Path.resolve()` then
   `Path.relative_to()` against the caller's home-directory tree, so `..`
   traversal or an absolute path outside the allowlist is rejected before
   any filesystem access), `open` (`os.startfile`). **Write/delete/move
   are not implemented** — `classify_risk()` returns `BLOCKED` and
   `validate()` raises `SkillValidationError` for any such action; nothing
   silently no-ops.
3. **`skills/browser.py` (`BrowserSkill`)** — `open_url`/`search` via
   stdlib `webbrowser` only; `validate_http_url()` rejects any scheme
   other than `http`/`https` (`file://`, `javascript:`, `data:`, etc. all
   rejected before `webbrowser.open` is ever called). No headless
   automation/scraping.
4. **`skills/terminal.py` (`TerminalSkill`)** — `run_command` via
   `subprocess.run` with real captured stdout/stderr/exit code and a
   timeout (default 30s, hard-capped at 120s regardless of caller input).
   `classify_command()` is pure/deterministic: a conservative denylist
   (`format`, `del`/`rd`/`rmdir /s`, `Remove-Item -Recurse -Force`,
   `shutdown`/`Stop-Computer`/`Restart-Computer`, `diskpart`, `reg
   delete`, `net user`, `bcdedit`, `vssadmin`, `cipher /w`, `runas`,
   `sudo`, `takeown`, remote `iex`/`Invoke-Expression` combined with a
   fetch construct, writes targeting `C:\Windows\System32`) is checked
   **before** a narrow leading-verb `READ_ONLY` allowlist (`dir`, `ls`,
   `type`, `cat`, `echo`, `whoami`, `pwd`, `cd`, `Get-ChildItem`,
   `Get-Process`, `Get-Content`, `git status`/`log`/`diff`) — and any
   chaining/redirection operator (`&`, `&&`, `|`, `;`, `>`, `<`)
   disqualifies a command from that allowlist entirely, so `dir & del /s`
   classifies `BLOCKED` (via the denylist) rather than slipping through as
   `READ_ONLY` because it starts with `dir`. `validate()` re-checks
   `BLOCKED` independently (defense in depth), and `execute()` refuses to
   actually run a `BLOCKED` command even if called directly.
5. **`skills/obsidian.py` (`ObsidianSkill`)** — `search`/`read` wrapping
   the **existing** `memory.service.MemoryService` (FTS5 + vault
   indexing) — no reimplemented indexing/search. `search` delegates to
   `MemoryService.search(..., source="vault")` and preserves its
   `source`/`source_id` fields verbatim into `ActionResult.data`. `read`
   resolves a vault-relative path with the same boundary-check pattern as
   `filesystem.py` and reads the real file (`MemoryService` only indexes
   snippets, not full bodies, so this is the one piece not already
   exposed by the existing service).
6. **`skills/voice_bridge.py`** —
   `build_action_request_from_voice(text, *, source, active_context=None)
   -> ActionRequest | None`. A small, fixed set of 4 regex patterns
   (`focus <target>`, `open <http(s) url>`, `search for <query>`,
   `launch/open/start <app>`) mirroring `assistant/router.py`'s
   deterministic-routing pattern (M2A criterion 12). Returns `None` for
   anything unrecognized — the caller (voice pipeline / action runtime,
   not this module) falls through to the existing LLM/assistant path
   unchanged. `assistant/router.py` itself was **not modified**.
7. **`skills/registry.py`** — `SKILLS: dict[str, Skill]` (eager; the 4
   skills above with no live-DB dependency) and `build_registry
   (memory_service=None, vault_path=None) -> dict[str, Skill]` for the
   full 5-skill registry including `obsidian`. See "Interfaces exposed"
   below for exactly why `obsidian` is not in the eager `SKILLS` dict and
   how to get it.

**Interfaces exposed** (for Codex, per M2A_INTERFACES.md's "expected
surface between streams"):

- `from skills.registry import SKILLS` → `dict[str, Skill]` with keys
  `windows_app`, `filesystem`, `browser`, `terminal` — importable and
  constructible with zero side effects, safe to import at module load
  time.
- `from skills.registry import build_registry` →
  `build_registry(memory_service: MemoryService | None = None, vault_path:
  str | None = None) -> dict[str, Skill]`. Call this **once you have a
  live `MemoryService` instance** (e.g. `request.app.state.memory_service`
  / `app.deps.get_memory_service`, the same DI pattern `app/main.py`'s
  lifespan and `app/deps.py` already use for every other request-scoped
  singleton) to get the complete 5-skill registry including `obsidian`.
  `obsidian` is deliberately excluded from the eager `SKILLS` dict because
  `ObsidianSkill` wraps `MemoryService`, which wraps a live
  `aiosqlite.Connection` that this codebase only creates during app
  startup — constructing it at import time would mean either an
  unmanaged second DB connection or a fabricated stand-in, both wrong.
- All five skill classes subclass `app.action_contracts.BaseSkill`
  unmodified — `classify_risk()`/`validate()`/`execute()` match the
  frozen `Skill` Protocol exactly; every `execute()` return is built via
  `self._result(...)` (never a hand-built `ActionResult`).
- `skills.terminal.classify_command(command: str) -> RiskLevel` and
  `skills.browser.validate_http_url(url: str) -> str` and
  `skills.filesystem.resolve_within_safe_roots(path: str) -> Path` are
  exposed as standalone functions (not just methods) in case the action
  runtime wants to pre-classify/pre-validate without a full skill dispatch
  — not required, just available.

**Files changed**:
`apps/backend/skills/__init__.py` (new),
`apps/backend/skills/windows_app.py` (new),
`apps/backend/skills/filesystem.py` (new),
`apps/backend/skills/browser.py` (new),
`apps/backend/skills/terminal.py` (new),
`apps/backend/skills/obsidian.py` (new),
`apps/backend/skills/voice_bridge.py` (new),
`apps/backend/skills/registry.py` (new),
`apps/backend/tests/test_skill_windows_app.py` (new),
`apps/backend/tests/test_skill_filesystem.py` (new),
`apps/backend/tests/test_skill_browser.py` (new),
`apps/backend/tests/test_skill_terminal.py` (new),
`apps/backend/tests/test_skill_obsidian.py` (new),
`apps/backend/tests/test_skill_voice_bridge.py` (new),
`apps/backend/tests/test_skill_registry.py` (new),
`apps/backend/requirements.txt` (one additive line: `pywin32==312`).
No existing V1 file's behavior was touched;
`apps/backend/app/action_contracts.py` and `apps/backend/app/contracts.py`
were read but not edited (frozen, per `M2A_SPEC.md`).

**Tests run** (from `C:\TARS-Wave2-Claude\apps\backend`, targeted only, per
`M2A_SPEC.md`'s validation cadence for this milestone):

- `python -m pytest tests/test_skill_windows_app.py
  tests/test_skill_filesystem.py tests/test_skill_browser.py
  tests/test_skill_terminal.py tests/test_skill_obsidian.py -q` — **113
  passed**, 0 failed (the exact five files named in the Phase 2 task spec).
- Same command plus `tests/test_skill_voice_bridge.py
  tests/test_skill_registry.py` — **124 passed**, 0 failed.
- Regression spot-check of pre-existing modules this work imports from —
  `python -m pytest tests/test_action_contracts.py
  tests/test_memory_service.py tests/test_memory_fts.py
  tests/test_memory_vault.py tests/test_contracts.py -q` — **25 passed**,
  0 failed (unmodified, none weakened).
- `python -m ruff check skills/ tests/test_skill_*.py` — **clean**.
- `python -m mypy --config-file pyproject.toml skills` — **clean, 8
  source files**.
- Full `apps/backend/tests` suite was **not** run this session (targeted
  validation per milestone cadence, not repeated full-suite runs per
  stream — see `M2A_SPEC.md`).

**Known limitations** (see `claude.done.json`'s `known_gaps` for the full
text):

- `focus`'s `SetForegroundWindow` path was verified against a **real**
  win32 window created in-process during the test run (genuine pass, not
  mocked) — it was **not** verified against a separately-running foreground
  application (e.g. a real, independently-launched Notepad) in this
  sandbox session, since that would mean popping/interacting with a second
  process's GUI window during an automated run. The same win32 API calls
  apply regardless of which process owns the target window, so this is a
  reasonable-confidence gap, explicitly flagged rather than omitted —
  never claim this as fully end-to-end certified.
- `terminal`'s `execute()` shells out via `cmd.exe`
  (`subprocess.run(..., shell=True)`); PowerShell-only cmdlets in the
  `READ_ONLY` allowlist (`Get-ChildItem` etc.) classify correctly but will
  genuinely fail at execution time unless wrapped in
  `powershell -Command "..."` — an honest `FAILED` result with real
  stderr, not a silent lie, but worth the HUD/runtime knowing about.
- `filesystem`'s safe-root allowlist is intentionally just the home
  directory tree (`Path.home()`) — no mechanism yet to add more roots.
- `voice_bridge` recognizes exactly 4 phrase shapes by design (not a
  general NLU system).

**Exact dependencies required from other agents**:

- **Codex** (`apps/backend/actions/`): import `skills.registry.SKILLS` for
  the 4 DB-independent skills, and call
  `skills.registry.build_registry(memory_service=<its MemoryService
  instance>)` to get the full 5-skill registry including `obsidian` — the
  action runtime is the natural place to obtain a live `MemoryService` the
  same way `app/main.py`'s lifespan does today.
- **Codex**: the permission engine must be the actual enforcement point
  for `RiskLevel` — a skill's own `BLOCKED` handling (e.g. `terminal.py`
  refusing to run a `BLOCKED` command even if `execute()` is called
  directly) is defense-in-depth, not a substitute for the runtime
  re-deriving/checking the classification itself, per
  `M2A_INTERFACES.md`'s RiskLevel section.
- No blocking dependency on Antigravity's native shell for this phase.

**Next recommended action**: Codex wires `skills.registry.SKILLS` /
`build_registry()` into `apps/backend/actions/` dispatch. Once that exists,
recommend an end-to-end action-runtime→skills dispatch test as part of the
Phase 4 integration validation gate.

---

## Append-only event history fix (2026-08-16)

**Branch**: `fix/v1-final-lifecycle`
**Base SHA**: `9f13e09a7c44be27901d34cb885a63ceb8755463`
**Final SHA**: `bfb39df` (see `git log 9f13e09..bfb39df` for the full diff)

Not merged into `feature/v1-remediation-integration`, `integration/v1`, or
`main` — that remains a coordinator decision.

**Commits**:

| SHA | Summary |
|---|---|
| `224ea47` | Enforce append-only event persistence |
| `bfb39df` | Add duplicate/concurrency regression tests for event persistence |

**Work completed** — fixed the final backend certification blocker:
`trading_events` was not genuinely append-only.

**Root cause**: `EventService._persist` (`events/service.py`) wrote every
event with `INSERT OR REPLACE INTO trading_events ... `, keyed on
`event_id` (the table's `PRIMARY KEY`, unchanged from Wave 1). `event_id`
is a required, caller-suppliable field on the public
`POST /api/v1/events` route (`app/routers/events.py`) — the raw JSON body
is validated against the frozen contract and passed straight into
`TradingEvent(**raw)`, which accepts an explicit `event_id`. So any
resubmission of an existing `event_id` — a retried request, a buggy
upstream integration, or a malicious caller — silently overwrote
(destroyed) that historical row instead of being rejected. The mock
generator and `/dev/mock-event` path were never affected (they always let
the model's `default_factory=uuid4` mint a fresh id), and the invalidate
endpoint already minted a fresh id too (fixed in a prior session) — the
open path was specifically direct callers of `POST /api/v1/events`
supplying their own `event_id`.

**Persistence change** (`events/service.py`): switched `_persist` to a
plain `INSERT` and let the existing `PRIMARY KEY` constraint enforce
uniqueness at the database level (no schema/migration change needed — the
constraint was already there, just unenforced by the upsert). Added
`DuplicateEventError(RuntimeError)`, raised when SQLite reports
`IntegrityError` on the insert, carrying the offending `event_id`. Because
`record_event` calls `_persist` before `_apply_active_state`, a rejected
duplicate never reaches the `active_setups` write — no partial-state
corruption path exists structurally, not just by convention.

**Duplicate behavior**: `app/routers/events.py` catches
`DuplicateEventError` in all three event-ingestion routes
(`POST /api/v1/events`, `POST /api/v1/events/{id}/invalidate`,
`POST /api/v1/dev/mock-event`) and returns **HTTP 409** with
`{"detail": "Event <id> already exists"}` — no SQLite/DB internals in the
response. The original historical row is left byte-for-byte unchanged
(verified in tests below).

**Lifecycle verification**: `tests/test_event_service.py::test_lifecycle_three_events_stay_queryable_and_active_state_clears`
drives `SETUP_DEVELOPING -> SETUP_VALID -> SETUP_INVALIDATED` directly
against `EventService` and asserts three distinct `event_id`s, all three
rows still present in `get_history()`, and `get_active_setups()` empty
afterward. (The API-level version of this same lifecycle proof already
existed from a prior session:
`tests/test_events_api.py::test_invalidation_preserves_prior_history_as_distinct_events`
— left unmodified, still passing.)

**Concurrency verification**: `tests/test_event_service.py::test_concurrent_duplicate_event_id_only_one_persists`
fires two `record_event()` calls for the same `event_id` via
`asyncio.gather(..., return_exceptions=True)`. Because `app/db.py`
intentionally uses one shared `aiosqlite` connection (documented there as
the correct scope for this single-user local app, not a pool), concurrent
writers are serialized through that connection's single worker thread —
so the database's `PRIMARY KEY` constraint deterministically admits exactly
one writer and raises `IntegrityError`→`DuplicateEventError` for the
other, with no TOCTOU window (no check-then-insert; the constraint itself
is the check). Test asserts exactly one success, one `DuplicateEventError`,
and exactly one matching row in history afterward.

**Files changed**: `apps/backend/events/service.py`,
`apps/backend/app/routers/events.py`,
`apps/backend/tests/test_events_api.py` (added 2 cases),
`apps/backend/tests/test_event_service.py` (new — 4 cases). No schema
migration, no change to the frozen trading-event contract, no change to
already-passing routes/behavior outside the duplicate-id path.

**Tests run**:

- `python -m pytest apps/backend/tests` — **81 passed** (up from 75 at
  session start; existing tests unmodified except the 2 additions to
  `test_events_api.py`, none weakened).
- `ruff check apps/backend` — **clean**.
- `mypy --config-file apps/backend/pyproject.toml apps/backend` — **clean,
  57 source files**.

**Known limitations**: none identified for this specific blocker. The
concurrency proof is deterministic given this app's single-shared-connection
architecture (see `app/db.py`'s own docstring); if that architecture ever
changes to a real connection pool, the same `PRIMARY KEY`-constraint
approach still holds (SQLite enforces it regardless of which connection
issues the `INSERT`), so no follow-up is required on that account.

**Next recommended action**: re-run final certification against
`fix/v1-final-lifecycle`. No other backend work is pending from this
session.

---

## Certification remediation (2026-08-16)

**Branch**: `fix/v1-backend-cert-blockers`
**Base SHA**: `e8308d82361e6df3c0928c4994ac0505a2e4235f`
**Final SHA**: `910c05a` (see `git log e8308d8..910c05a` for the full diff)

**Commits** (each pushed after its own test/lint/type-check pass):

| SHA | Summary |
|---|---|
| `76ed328` | Event lifecycle: SETUP_INVALIDATED no longer overwrites SETUP_VALID history |
| `45b548b` | Payload security: real streaming body-size limit, not Content-Length-only |
| `11d975d` | Memory grounding: source_id now flows into assistant grounding context |
| `d478172` | Real voice path: STT->assistant->TTS proof test with actual local models |
| `d611723` | Ruff: resolved remaining `apps/backend/run.py` E402 violations |
| `910c05a` | This handoff |

Not merged into `feature/v1-integration`, `integration/v1`, or `main` —
that remains a coordinator/Codex decision.

**Work completed** — fixed five Codex-identified backend certification
blockers, scoped strictly to `apps/backend/` plus the two shared-test call
sites that encoded the bugs being fixed (see below):

1. **Event lifecycle / invalidation** (`app/routers/events.py`). The
   invalidate endpoint reused the original event's `event_id`; since
   `trading_events.event_id` is the primary key behind an
   `INSERT OR REPLACE` upsert, invalidating a setup silently destroyed its
   `SETUP_VALID` row. Fixed by letting the invalidated event get its own
   unique `event_id` (the model default), correlating back to the original
   via an `ORIGINAL_EVENT_ID:<uuid>` reason code (the frozen trading-event
   contract has no dedicated correlation field — not touched).
   Regression test: `tests/test_events_api.py::test_invalidation_preserves_prior_history_as_distinct_events`
   drives `SETUP_DEVELOPING -> SETUP_VALID -> SETUP_INVALIDATED` over the
   real HTTP API and asserts three distinct `event_id`s, all three still
   present in history, and active state cleared.
   `tests/acceptance/test_runtime.py::test_event_lifecycle_reaches_two_clients_and_persists`
   (Codex-owned, shared) asserted the old, buggy id-reuse behavior; updated
   it to match by symbol instead of a pre-known id (the new id is
   server-generated) and added an explicit "SETUP_VALID still in history"
   check. Verified against a live `python apps/backend/run.py` process with
   `TARS_ACCEPTANCE=1`.

2. **Payload security** (`app/body_limit.py`, `app/main.py`). The old
   middleware only compared the declared `Content-Length` header against
   1 MiB — a no-op for chunked transfer encoding, which never sends that
   header. Replaced with `MaxBodySizeMiddleware`, a raw ASGI middleware
   that counts bytes as uvicorn delivers them via `receive()` (the same
   representation for Content-Length- and chunked-framed bodies) and
   aborts as soon as the running total exceeds the cap, without ever
   buffering the oversized body. The abort signal (`RequestBodyTooLarge`)
   is a `BaseException`, not `Exception`, subclass so it can't be silently
   swallowed by a route's broad `except Exception` around body reading
   (confirmed this actually happens in `events.py`'s JSON-parse handler
   during testing — an `Exception` subclass got turned into an unrelated
   422). Verified against both `TestClient` and a live uvicorn process
   with a real chunked-transfer request (no `Content-Length` header sent).
   Regression tests: `tests/test_payload_limit.py` (normal payload passes,
   Content-Length-declared oversized payload rejected, chunked oversized
   payload rejected, chunked under-limit payload reaches the route).

3. **Memory grounding / source traceability** (`assistant/grounding.py`).
   `build_system_context` dropped `source_id` (the vault file path /
   conversation `message_id`) from retrieved FTS notes before handing them
   to the assistant provider — only the generic `source` type
   ("vault"/"conversation") survived, so a grounded answer could never be
   attributed to which specific note backed it. Fixed by passing
   `source_id` through and instructing the provider to cite it. No change
   to the frozen assistant-message contract (no field exists for
   memory-note citations; traceability lives in the grounding text, not
   response metadata — this was evaluated and doesn't rise to "genuine
   blocker" for a contract change). Regression tests:
   `tests/test_assistant_grounding.py` — `source_id` survives into the
   context payload sent to the provider (verified with a stub provider
   that echoes back what it received), and absent-information disclosure
   for both empty and irrelevant retrieval.

4. **Real voice backend path** (`tests/test_voice_end_to_end_real.py`).
   The existing implementation (`voice/pipeline.py`,
   `voice/pipecat_bridge.py`, `voice/pipecat_services.py`,
   `voice/providers/faster_whisper_stt.py`,
   `voice/providers/kokoro_tts.py`) was already architecturally correct —
   real STT -> `AssistantRouter` -> real TTS, no canned phrases — so no
   production code changed here. Added a proof test using actual model
   weights: Kokoro synthesizes "What setups require my attention?" into a
   real WAV fixture, faster-whisper independently transcribes it, the
   transcription (not the hardcoded phrase) is sent through
   `AssistantRouter` over the real `/api/v1/assistant/query` HTTP
   endpoint, and the reply is synthesized back to playable audio. A
   second, distinct control phrase must decode to distinct text — a
   bypassed/canned STT stage would return identical text regardless of
   audio, so the test fails in that case. **Executed with real models in
   this environment**: `faster-whisper==1.2.1` and `kokoro-onnx==0.5.0`
   installed successfully and both model weights downloaded and ran (CPU,
   `base`/`af_heart`). `pipecat-ai` (the realtime WebSocket voice session)
   was *not* installed/exercised this session — heavier dependency, and
   the proof requirement only concerns the STT->assistant->TTS data path,
   not the realtime transport; `voice/pipeline.py` etc. were reviewed by
   inspection only, not executed. `scipy` added to
   `requirements-voice.txt` as a test-only dependency (resamples Kokoro's
   24kHz output to whisper's 16kHz input).

5. **Ruff**: `apps/backend/run.py` had 2 pre-existing E402 violations
   (sys.path manipulation before `import uvicorn`/`app.config`, which is
   necessary when run as a script). Fixed with targeted `# noqa: E402` on
   those two lines rather than disabling the rule project-wide.
   `ruff check .` from `apps/backend/` is now clean.

**Files changed**: `apps/backend/app/main.py`, `apps/backend/app/body_limit.py`
(new), `apps/backend/app/routers/events.py`, `apps/backend/assistant/grounding.py`,
`apps/backend/run.py`, `apps/backend/requirements-voice.txt`,
`apps/backend/tests/test_events_api.py`, `apps/backend/tests/test_payload_limit.py`
(new), `apps/backend/tests/test_assistant_grounding.py` (new),
`apps/backend/tests/test_voice_end_to_end_real.py` (new); plus
`tests/acceptance/test_runtime.py` (Codex-owned — edited only to correct
an assertion that encoded the event-lifecycle bug being fixed; flagged
here rather than left silently unowned). This handoff file only under
shared coordination docs.

**Tests run**:

- `python -m pytest apps/backend/tests` — **75 passed, 0 skipped** (up
  from 61 passed/1 skipped at session start — the previously-optional
  `test_voice_real_adapters.py` cases now execute for real since
  faster-whisper/kokoro-onnx are installed in this environment).
- `ruff check .` (from `apps/backend/`) — **clean**.
- `mypy .` (from `apps/backend/`, using its own `pyproject.toml`) —
  **clean, 57 source files**. (Note: running `mypy apps/backend` from the
  repo root instead picks up no config and reports ~20 spurious
  `import-untyped`/`import-not-found` errors for packages the project
  deliberately leaves untyped/optional, e.g. `pipecat`, `openwakeword`,
  `jsonschema` — always run mypy from `apps/backend/` or with
  `--config-file apps/backend/pyproject.toml`.)
- `python -m pytest tests -q` (Codex's contract/unit suite) —
  **39 passed, 15 skipped, 1 failed**. The one failure
  (`tests/contracts/test_codegen_drift.py::test_generated_contracts_have_no_drift`)
  is a pre-environment gap unrelated to this session's changes: it shells
  out to `npm ci --prefix tools/codegen`, and `tools/codegen/node_modules`
  was never installed in this workspace. Confirmed pre-existing via
  `git stash` before investigating further — not a regression.
- Targeted acceptance tests against a manually-started live
  `python apps/backend/run.py` process with `TARS_ACCEPTANCE=1`:
  `test_event_lifecycle_reaches_two_clients_and_persists`,
  `test_websocket_disconnect_then_reconnect`,
  `test_unexpected_event_field_is_rejected`,
  `test_oversized_payload_is_rejected`, `test_no_live_trade_execution_surface`,
  `test_voice_reports_real_local_provider_path`,
  `test_assistant_refuses_to_invent_missing_trading_data`,
  `test_public_event_response_cannot_bypass_contract` — **all passed**.
  The *full* `tools/run_acceptance.py` suite (15/15) was not re-run:
  `apps/web/node_modules` is not installed in this workspace, and
  installing/running the frontend is out of scope for a backend-only
  remediation session per the task's "do not modify frontend files"
  instruction.

**Known limitations**:

- **Python version**: this machine has Python 3.13.3 and 3.7 installed,
  no 3.12 (`py -0p` confirms). All validation above ran under **3.13.3**.
  The project's `pyproject.toml` target (`py312`/mypy `python_version =
  "3.12"`) was left unchanged. This is *not* a certification of Python
  3.12 compatibility — someone with a 3.12 interpreter should re-run
  `pytest`/`ruff`/`mypy` before treating this as 3.12-verified.
  faster-whisper/kokoro-onnx/scipy installed successfully on 3.13 in this
  environment; that says nothing about 3.12 specifically.
- `pipecat-ai` (realtime voice WebSocket session) remains uninstalled and
  unexercised this session — `voice/pipeline.py`,
  `voice/pipecat_bridge.py`, `voice/pipecat_services.py` were reviewed by
  code inspection only, found architecturally correct (no canned
  phrases/faked success anywhere in the chain), but not run.
- `tools/codegen` npm deps and `apps/web` npm deps are both uninstalled in
  this workspace, so the codegen-drift test and the full black-box
  acceptance suite (which needs a live frontend) could not be run this
  session. Backend-only acceptance checks were run individually against a
  manually-started backend instead (see Tests run above).

**Exact dependencies required from other agents**: none — this was a
backend-only remediation pass against blockers Codex already identified.
If Codex re-certifies, the event-lifecycle broadcast for an invalidated
setup now carries a **new** `event_id` (not the original's); any external
consumer/tooling that assumed id-reuse (as `tests/acceptance/test_runtime.py`
did) should match by symbol instead, per the fix in commit `76ed328`.

**Next recommended action**: Codex re-certifies against
`fix/v1-backend-cert-blockers`. Before merging toward `integration/v1`,
someone should (a) validate under an actual Python 3.12 interpreter, (b)
install `apps/web` and `tools/codegen` npm deps and run the full
`tools/run_acceptance.py` 15-case suite plus the codegen-drift check, and
(c) optionally install `pipecat-ai` and exercise the realtime voice
WebSocket session directly (this session only proved the STT->assistant->TTS
data path, not the realtime transport around it).

---

## Wave 1 backend implementation (2026-08-16)

**Branch**: `feature/v1-backend-voice`
**Base SHA**: `ad6f4bf6bd4dcb5c4039450dc8b8540ce63108e7` (tip of `integration/v1` at session start)
**Final SHA**: `2bd64f664bf659085c67d3660ca21931ea06305a`

**Commits** (each pushed after its own test/lint/type-check pass):

| SHA | Summary |
|---|---|
| `f1fc132` | Core event backend: FastAPI + WebSocket + SQLite (Phase A) |
| `88fd015` | Assistant provider abstraction + deterministic routing (Phase B) |
| `1d7fda0` | Memory/retrieval layer: SQLite FTS5 + Obsidian vault indexing (Phase C) |
| `df2a9e1` | Voice pipeline: Pipecat + local STT/TTS/wake-word providers (Phase D) |
| `bea3c14` | Voice/assistant API surface + secure-by-default networking (Phase E+F) |
| `2bd64f6` | OpenTelemetry instrumentation (ADR-017) |

All six commits are on `feature/v1-backend-voice` and pushed to
`origin/feature/v1-backend-voice`. Nothing merged into `integration/v1` or
`main` — that is a coordinator decision per `AGENTS.md`.

## Work completed

Implemented the full backend mission (Phases A–F) plus OpenTelemetry
instrumentation from `TASKS.md`:

- **Phase A — Core event backend.** FastAPI app (`app/`), SQLite schema +
  migration runner (`storage/`), mock trading-event generator
  (`events/generator.py`), deterministic active-state calculation
  (`events/service.py`), WebSocket broadcast (`/ws/events`). Every event is
  validated directly against `contracts/trading-event.schema.json` via
  `jsonschema` (`app/contracts.py`) — not a hand-duplicated copy — so the
  backend can never silently drift from the frozen contract.
- **Phase B — Assistant tool layer.** `AssistantProvider` interface with
  four adapters: `MockAssistantProvider`, `ClaudeCodeProvider` (headless
  `claude -p --output-format json` CLI invocation, verified against current
  Claude Code docs, not assumed), `OllamaProvider` (local HTTP), optional
  `AnthropicAPIProvider` (paid, lazy-imports `anthropic`). Deterministic
  routing (`assistant/router.py`) resolves "show active setups" / "what
  requires my attention" straight from `EventService` — these never reach a
  model call. Everything else gets deterministic active-setup state as
  grounding text instructing the model never to invent trading facts.
  Conversation turns persist to `conversation_messages`, validated against
  `contracts/assistant-message.schema.json`.
- **Phase C — Memory.** `MemoryService` (`memory/`) with SQLite FTS5 full-
  text search over conversation memory and an indexed Obsidian vault
  (read-only indexer, content-hash-gated, excludes `.obsidian`/`.git`).
  Every search result carries `source`/`source_id` for traceability.
  Per ADR-013, `MemoryService` refuses to start with
  `SQLITE_VEC_ENABLED=true` rather than silently ignoring it — no
  measurement has justified semantic retrieval over FTS5 yet. APScheduler
  wired for periodic vault reindexing (housekeeping only, per ADR-016).
- **Phase D — Voice.** `WakeWordProvider`, `SpeechToTextProvider`,
  `TextToSpeechProvider` interfaces, each with a zero-dependency mock plus
  real local adapters: `FasterWhisperSTTProvider`, `KokoroTTSProvider`
  (kokoro-onnx, fully local), `FishSpeechTTSProvider` (HTTP client against
  a separately-run local Fish Speech API server), `OpenWakeWordProvider`.
  The realtime pipeline (`voice/pipeline.py`) is built on pipecat-ai
  1.7.0's current `PipelineWorker`/`WorkerRunner` API — verified against
  the installed package's actual module structure via introspection, not
  assumed from training data, since 1.7.0 deprecated the
  `PipelineTask`/`PipelineRunner` API shown in older Pipecat examples.
  Generic `ProviderBridgeSTTService`/`ProviderBridgeTTSService` wrap our
  own provider instances rather than Pipecat's vendor-specific services, so
  the realtime session and the one-shot REST endpoints share exactly one
  adapter instance per provider. Silero VAD needs no separate package —
  bundled inside `pipecat-ai` as an ONNX model.
- **Phase E — Voice/assistant API.** `POST /api/v1/voice/transcribe`
  (WAV → text), `POST /api/v1/voice/synthesize` (text → WAV),
  `GET /api/v1/voice/status` (provider readiness/names, never secrets),
  `WS /api/v1/voice/session` (realtime audio↔audio over the Phase D
  pipeline). Text question/response already existed from Phase B
  (`POST /api/v1/assistant/query`). STT/TTS/wake-word providers load in a
  background task (`app/voice_state.py`) so cold model downloads (minutes,
  see Benchmarks below) never block app startup; health/events/assistant
  work immediately.
- **Phase F — Local network.** `Settings.effective_host` resolves to
  `127.0.0.1` by default; `BIND_LAN=true` opts into `0.0.0.0`; an explicit
  `BACKEND_HOST` always wins. Tailscale Serve (the preferred private
  remote-access path, ADR-014) works against the loopback default without
  needing LAN exposure. `CORS_ALLOW_ORIGINS` is configurable (default `*`).
  `run.py` is the settings-aware uvicorn entrypoint. Verified with a real
  server smoke test: bound `127.0.0.1:8000`, `/api/v1/health` and
  `/api/v1/voice/status` both responded correctly.
- **OpenTelemetry (ADR-017).** `app/observability.py` configures a
  `TracerProvider` that logs spans through the stdlib logger by default (no
  network dependency) or exports to OTLP if `OTEL_EXPORTER_OTLP_ENDPOINT` is
  set. Instruments event ingestion, assistant requests (provider, latency,
  errors), and memory retrieval (retrieved `source:source_id`s). Verified
  end-to-end — spans logged correctly with no secrets in any attribute.

## Files changed

~80 files under `apps/backend/` (new: `app/`, `storage/`, `events/`,
`assistant/`, `memory/`, `voice/`, `tests/`, `run.py`, `README.md`,
`requirements*.txt`) plus additive edits to `.env.example` (root-level
config template — not under `docs/coordination/` or `contracts/`, and
already owns the backend env vars this session extended; edited
additively only, no existing key removed or renamed without a documented
reason). See the six commits above for the exact diff per phase.

## Interfaces exposed

**HTTP** (all under `/api/v1`):
- `GET /health`
- `GET /events`, `GET /events/active`, `POST /dev/mock-event`
- `POST /assistant/query`
- `GET /memory/search`, `POST /memory/reindex-vault`
- `POST /voice/transcribe`, `POST /voice/synthesize`, `GET /voice/status`

**WebSocket**:
- `/ws/events` — trading-event + active-setup broadcast
- `/api/v1/voice/session` — realtime voice (Pipecat pipeline)

**Python interfaces** (for future backend work, not external consumers):
`assistant.provider.AssistantProvider`, `voice.interfaces.WakeWordProvider`
/ `SpeechToTextProvider` / `TextToSpeechProvider`, `memory.service.MemoryService`.

## Environment/configuration

All new settings are documented in `.env.example` at the repo root and
`apps/backend/app/config.py` (kept in lockstep per that file's own
docstring rule). Defaults are the free/local-first path:
`ASSISTANT_PROVIDER=claude_code`, `STT_PROVIDER=faster_whisper`,
`TTS_PROVIDER=kokoro`, `WAKE_WORD_PROVIDER=mock` (see Known limitations),
`BIND_LAN=false`. Two new requirements files: `requirements-voice.txt`
(pipecat-ai + local model packages — optional, every voice provider mocks
without it) and `requirements-optional.txt` (paid-provider SDKs — never
required).

## Benchmarks (Phase D)

Measured in this sandboxed dev environment (CPU-only, no GPU):

| Adapter | Cold init (incl. first-run download) | Warm init | Generation latency |
|---|---|---|---|
| faster-whisper (`tiny`) | 48.98s | ~2.3s | 0.70s for 1s of audio |
| Kokoro TTS | 187.97s | ~2.5s | 8.66s cold / 0.77s warm, for a one-sentence reply |
| Fish Speech (local) | **not benchmarked** — see below | — | — |

**Fish Speech could not be benchmarked in this environment.** It has no
pip-installable inference API; running it requires cloning the
`fish-speech` repo, downloading GB-scale checkpoints, and running a
separate local API server (`tools/api_server.py`) this sandbox had no
practical way to host. `FishSpeechTTSProvider` is implemented as a
best-effort HTTP client against that server's documented `/v1/tts`
endpoint, but its exact request/response JSON schema was not fully
published where I could reach it — verify against the actual deployed
server version before relying on it. **Kokoro is the default `TTS_PROVIDER`
in `.env.example`** on this evidence: fully local, no separate process, and
fast once warm. If Fish Speech is benchmarked on real target hardware
(ideally with a GPU) and found to sound meaningfully better, flipping the
default is a one-line `.env` change — the provider is fully implemented.

## Tests

63 pytest cases across `apps/backend/tests/`, all passing; `ruff check .`
and `mypy app events storage assistant memory voice run.py` both clean.
Covers: contract validation, event persistence/active-state/invalidation,
WebSocket broadcast, deterministic assistant routing (proven to never call
the configured provider), assistant provider fallback-to-mock on
misconfiguration, memory FTS5 round-trip + vault indexing lifecycle, voice
mock providers, audio WAV↔PCM16 conversion, voice provider factory error
paths, network config resolution, and — the only tests touching real ML
models rather than mocks — `tests/test_voice_real_adapters.py`, which
exercises faster-whisper and Kokoro against actual (already-cached in this
environment) model weights; these skip cleanly via `pytest.importorskip`
if the voice extras aren't installed, so the rest of the suite needs no
network and no API keys.

**Not run**: no live audio round-trip through `/api/v1/voice/session` —
that needs a real microphone/browser client, which Antigravity owns. The
pipeline was verified by introspecting the installed `pipecat-ai` 1.7.0
package's real API (class names, constructor signatures, method
signatures) rather than assumed, and by the unit-level construction tests,
but not by an actual end-to-end voice conversation.

## Known limitations

- **No custom "TARS" wake-word model.** openWakeWord ships no pretrained
  model for the phrase "TARS" (only generic phrases like "hey jarvis").
  `OpenWakeWordProvider` is fully implemented and will run any
  custom-trained model pointed at by `WAKE_WORD_MODEL_PATH`, but training
  one is a separate, later effort (openWakeWord's own training pipeline,
  requiring assembled audio samples). `.env.example` defaults
  `WAKE_WORD_PROVIDER=mock` for this reason. Push-to-talk is unaffected —
  `/api/v1/voice/session` never requires wake-word detection to begin a
  session, satisfying AGENTS.md's "guaranteed regardless of wake-word
  state" requirement structurally.
- **Fish Speech not benchmarked** — see Benchmarks above.
- **Wake word is not wired into the realtime pipeline as a gate.** The
  `WakeWordProvider` interface and `OpenWakeWordProvider` adapter are
  implemented and unit-testable, but `voice/pipeline.py`'s realtime session
  doesn't yet use them to gate when STT starts listening — the pipeline is
  push-to-talk/continuous-session by design (the client decides when to
  open the WebSocket). Wiring wake-word detection as an always-on gate
  ahead of a real trained model existing seemed like the wrong order of
  operations; flagging it as intentionally deferred, not an oversight.
- **No live voice round-trip test** — see Tests above.
- **Memory search is not wired into assistant grounding.** Phase C built a
  complete, working retrieval service (FTS5 + vault + API), but Phase B's
  `AssistantRouter` only grounds on deterministic trading state, not on
  retrieved memory/vault context. Wiring "recall relevant past notes into
  the assistant's context" is a natural next step, deliberately not done
  here to avoid scope creep into a Phase B change under a Phase C commit.
- **`fastapi`/`starlette` were upgraded** from `0.115.0`/(implicit) to
  `0.141.1`/`1.6.0` mid-session — `pipecat-ai[websocket]` pulled in the
  newer versions as a transitive dependency, and I re-pinned
  `requirements.txt` to match after confirming the full test suite still
  passes at the new versions. Flagging this since it's outside the diff
  Phase A originally shipped.
- **Python 3.13 was used**, not the 3.12 named in `AGENTS.md`/`ARCHITECTURE.md`
  — this machine only had 3.13 and 3.7 available (`py -0` confirmed no 3.12
  install). Nothing in the codebase is 3.13-specific; it should run
  unmodified on 3.12 if that's later required, but this wasn't verified on
  3.12 in this session.

## Exact dependencies required from other agents

- **Antigravity (frontend)**: the WebSocket/HTTP contracts above are
  stable and ready to consume. For voice specifically:
  `/api/v1/voice/session` expects 16-bit PCM mono audio in and returns the
  same out (no WAV wrapping over the wire — `add_wav_header=False`); WAV
  wrapping is only used by the one-shot `/api/v1/voice/transcribe`
  (upload) and `/api/v1/voice/synthesize` (response) endpoints. Browser
  `MediaRecorder` typically produces webm/opus, not raw PCM — the frontend
  is responsible for any needed transcoding before hitting these
  endpoints; this backend does not transcode.
- **Codex (tests/tools)**: `apps/backend/README.md` documents how to run
  the test suite and what's expected to work with zero API keys (the
  `mock` provider path for assistant/STT/TTS/wake-word — confirmed via
  `tests/test_assistant_provider_fallback.py` and the voice mock tests).
  The `contracts/*.schema.json` validation lives in `app/contracts.py`,
  loading the canonical files directly (no forked copy) — useful if Codex
  wants to cross-check contract conformance independently.

## Next recommended action

1. A coordinator decides whether/when to merge `feature/v1-backend-voice`
   into `integration/v1`, per `AGENTS.md`'s merge-strategy note in
   `TASKS.md` (Cross-cutting / Post-Wave-1).
2. If someone has GPU hardware available, benchmark Fish Speech properly
   (run the local API server, compare against the Kokoro numbers above)
   before treating the current `TTS_PROVIDER=kokoro` default as final.
3. Training a real "TARS" openWakeWord model, if always-on wake-word
   listening (vs. push-to-talk) becomes a priority.
4. A live voice round-trip test once Antigravity has a client that can
   open `/api/v1/voice/session` with a real microphone.
5. Wiring memory search into assistant grounding (Known limitations above)
   if "recall past notes" becomes a desired assistant capability.
