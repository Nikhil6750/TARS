# Independent data-consistency verification — 2026-11-01

**FAIL / NO-GO.** The two prior data consistency defects are fixed and cold
storm latency passes, but publication can remove data coverage or lose a
concurrent update. Cancellation during the new initial subscription wait also
leaks the receiver task. No backend production files were edited.

Source: `fix/market-data-revision-consistency`, exact fetched commit
`cfcdf37a43189bd3e3033254aaeab3a9c15691e2`.
Independent branch/worktree: `verify/market-data-consistency-final` /
`C:\TARS-Market-Consistency-Verify`. No Tauri rebuild, UI change, or merge.
Independent regression implementation: `8d8df7a2f7d26de5fabfa5f50447f78d1ce5b869`.

## Actual results

| Verification | Result |
| --- | --- |
| New backend revision-consistency file | 10 passed |
| Original standalone-import and concurrent feed-write reproductions, plus real-build disconnect test | 3 passed; prior xfails removed after actual passing runs |
| Real Uvicorn independent API | 15/15 passed |
| Real Uvicorn API-only Market acceptance | 16/16 passed |
| Production lifecycle/shared-frame tests rerun | 71 passed, 3 failed; causes below |
| Complete backend suite | Attempted; three failures, then stalled in TestClient shutdown at reported 71%; interrupted after watchdog diagnosis. No completed-suite PASS claimed |
| Remaining backend files, response-quality onward | 543 passed, exit 0, 239.42s |
| Root regression | 79 passed, 24 skipped, 3 confirmed-defect strict xfails |
| Ruff on independent tests / whitespace | Passed |

The independent feed test now starts a real writer thread during the real panel
build. The old synchronous callback wrote reentrantly on the builder's own
thread, which cannot represent the separate production feed worker once the
lock is reentrant. The writer is proven blocked until all panels finish, then
commits; panel prices agree. Standalone subprocess import now causes the real
routes to replace their service, and new WebSocket/HTTP prices agree. A reader
retaining the previous generation still returns its original immutable data.
These isolated fixtures never modify or manufacture product history.

Existing legacy store compatibility was exercised by all real-store HTTP and
storm checks against `D:\TARS_DATA\market\xray_store` (18 instruments, flat
layout and no generation marker). New generation stores were exercised by
actual subprocess imports and the backend's generation tests. Replay bounds,
provenance and execution-disabled API checks passed. Authenticated broker LIVE
behavior remains unverified; this machine's terminal lacks authentication.

## Cold reconnect measurements

```powershell
python -u tools/measure_market_ws_storm.py --clients 120 --repeats 3 --soak-storms 30 --report artifacts/consistency-storm/results.json
```

Actual application / Uvicorn / real historical candle store, run before the
full backend suite to avoid competing test load. Cursor advances to a new key
each run; the harder mixed-timeframe case moves it again after the abort storm.

- Cold first frame after 120 aborts: **2.0548, 0.9796, 0.8223 seconds**.
- Mixed-timeframe cold probes: **0.9768, 0.9624, 0.8475 seconds**.
- Peak concurrent builds: **1**. Identical cold storms each require one build.
- Zero server ERROR records, client errors or frame-integrity problems.
- Connected bystander: two pushes in 25s, largest gap 10.0296s (normal interval
  10s). Health max 0.0785s.
- Thirty additional storms: after every settle, zero handlers, connections,
  flights or active builds. Retention remains 16; threads move from 61 to 59.
- Settled RSS roughly 225–280 MB after GC; final 271 MB. Endpoint-based
  second-half growth is 43.3 MB because samples oscillate; this bounded run
  establishes neither a monotonic leak nor a long-term memory guarantee.
- `trading_execution=DISABLED` at start and finish.

The normal storm path has no send-after-close errors. The separate handler
cancellation test below exposes an untested lifecycle boundary. OS file cache
was not flushed; these are cold *frame-cache* measurements.

Real Uvicorn concurrent immediate subscriptions: M5 first=M5 (3.142s), H1
first=H1 (3.867s), H4 first=H4 (4.416s). These ran during backend regression
load and all returned RECORDED_STORE. `first-subscriptions.log` records them.

## Confirmed defects and focused corrections for Claude

1. **Failed replacement removes previously usable coverage.**

   ```powershell
   python -m pytest tests/integration/test_market_generation_publication.py -k failed_target --runxfail -q
   ```

   Import valid EURUSD and GBPUSD test CSVs; remove only the GBPUSD replacement
   source; target both again. EURUSD succeeds, GBPUSD is reported failed, but
   the active store now contains only EURUSD. Old GBPUSD files still exist in
   the previous generation, yet the new manifest no longer exposes them.
   `ingest_all` preserves old datasets only when their symbol is *not in the
   target list*, instead of when a replacement was not successfully completed.
   Preserve previous usable datasets for failed replacements (with truthful
   warnings), or fail publication and keep the whole old generation.

2. **Concurrent targeted publishers lose successful updates.**

   ```powershell
   python -m pytest tests/integration/test_market_generation_publication.py -k concurrent --runxfail -q
   ```

   Begin with EURUSD. Concurrent imports add GBPUSD and AUDUSD. A barrier just
   before publication forces both to build from the same base. Both succeed,
   but the final manifest exposes only EURUSD and one addition. The test
   serializes only the final OS renames to avoid simultaneous Windows handle
   interference; it does not change import results or base selection.
   Atomic rename prevents partial JSON, not lost updates. Provide cross-process
   publication coordination/rebase or explicit conflict rejection. In the first
   unrestricted simultaneous-rename run, Windows also returned WinError 5 to
   one publisher; the serialized reproduction proves lost updates independently.

3. **Cancellation during the initial subscription wait leaks a receiver.**

   ```powershell
   python -m pytest tests/integration/test_market_ws_initial_cancel.py --runxfail -q
   ```

   Start a handler, wait until receive_json is pending, cancel during the 100ms
   initial-subscription wait, then compare asyncio tasks. `receive_loop` remains
   alive after the handler exits. The test cleans that task after capturing it.
   The new wait sits before the try/finally that cancels/gathers the receiver.
   Move all post-receiver setup under guaranteed cleanup, preserving cancellation
   propagation and both subscribed/default first-frame behavior.

All three regressions were first run as ordinary tests and failed on the
specific defects above. They are now strict xfails to keep known failures
visible; use `--runxfail` for their original failing assertions. Production
corrections remain Claude's ownership.

## Backend suite failures: test assumptions need repair

The focused rerun reproduces three failures:

- `test_each_subscription_gets_its_own_timeframe`: it cancels both clients as
  soon as the subscribed H4 client receives a frame. The unsubscribed H1 client
  is still in its new 100ms initial wait. Wait for both clients' first frames.
- `test_an_unknown_timeframe_is_ignored_as_before`: it disconnects after 60ms,
  before the new 100ms fallback wait expires, then indexes an empty sent list.
  Await the actual fallback frame before disconnecting.
- `test_stress_aborting_at_every_phase_of_a_frame`: every abort delay is
  0–78ms, before the new 100ms wait ends. Its `total builds > 0` assertion fails
  because no build starts. Add clients whose lifetimes cross the initial wait
  and synchronize with build entry to retain genuine frame/disconnect coverage.

The nominally passing `test_clients_arriving_together_build_one_service` also
emits unhandled thread exceptions: its Stub lacks `.store`, now required by
generation checking. Repair the stub and assert every thread completed without
an exception; a count assertion alone does not establish successful adoption.
These backend test changes are assigned to Claude; no backend tests were edited.

The full default backend invocation was `python -m pytest -q --durations=20
-o faulthandler_timeout=240`. It stalled at
`test_response_quality_v2.py::test_v1_query_is_the_only_canonical_presentation_endpoint (teardown)`.
The 240-second watchdog shows pytest in Starlette `wait_shutdown`, with the
application loop idle in Windows IO polling; its process CPU stopped advancing.
The active test was independently identified from that process's
`PYTEST_CURRENT_TEST` environment field. This test passed alone in 9.55s, so the
underlying accumulated-state shutdown cause is unresolved, not attributed to
the market fix. The hung run was stopped and the tail from
`test_response_quality_v2.py` onward was run separately (`backend-tail.log`).
That remaining-file run passed **543 tests** in **239.42 seconds**, including
the previously stalled test and real local voice adapters.
This additional execution does not turn the interrupted run into a green full
suite. Claude should investigate the shutdown wait if it recurs; no speculative
shutdown change was made here. `shutdown-isolated.log` records the isolated pass.

## Immutable generation disk and partial-publication review

Manifest publication is atomic after successful file writes. Old readers pin
their generation. Untouched symbols are hardlinked, falling back to copy on
filesystems without hardlink support. An independent actual-import fixture
confirmed two retained generations, 24,776 logical Parquet bytes, unchanged GBPUSD
hardlinked and replaced EURUSD separately allocated (`generation-disk.log`).

No generation reclamation exists. Old full generations, initial legacy files,
and unpublished generations after some failures can remain indefinitely. The
real flat store totals **112,543,045 bytes** across 91 files; each full reimport
allocates another generation of replaced datasets (roughly that order of size
for the same data, not a measured production reimport). No real store was
rewritten or deleted for this review. Disk-full recovery/reclamation was not
destructively tested. Define a safe retention/reclamation policy before repeated
automatic imports; existing readers must remain protected.

Evidence: `full-backend.log`, `new-regression.log`, `ws-focused-failures.log`,
`independent-reproduction.log`, `generation-reproduction.log`,
`initial-cancel-repro.log`, `generation-disk.log`, `first-subscriptions.log`,
`artifacts/consistency-api.json`, `consistency-storm.log`, and
`artifacts/consistency-storm/results.json` in this worktree.
