# Final WebSocket performance verification — 2026-11-01

**FAIL / NO-GO for integration:** reconnect performance passes, but a confirmed
standalone-import cache invalidation defect can serve incorrect replay panels,
and a feed correction during generation can mix panel revisions in one frame.
`release/market-xray-final-rc` remains unchanged at `1939dc2`.

Reviewed branch: `fix/market-ws-reconnect-storm`, exact tip
`a3a27e8d636a02af42097057c26497f822184cff` (implementation `ae917ae8cb31959e39eb8375f27a34a52ba2f15d`).
Independent worktree: `C:\TARS-Market-WS-Verify`, branch
`verify/market-ws-reconnect-final`. Test implementation:
`c04ba145ed7eec939c64cd4ae10f02f5f1856dc4`.
Feed-revision regression: `e62d30b760339c11e4f9723daea49e70ad56b213`.

## Performance and resource evidence

Executed against the actual FastAPI application under real Uvicorn, reading the
real 18-instrument historical store (no substitute or generated product history):

```powershell
python -u tools/measure_market_ws_storm.py --clients 120 --repeats 3 --soak-storms 30 --report artifacts/independent-ws-storm/results.json
```

| Check | Observed | Verdict |
| --- | --- | --- |
| Cold first frame after 120 aborts, new cursor for every run | 2.5484, 0.7530, 0.7434 seconds | PASS, all below 5s |
| Cold probe behind mixed-timeframe storm, cursor moved again after aborts | 1.5021, 1.4071, 1.5634 seconds | PASS |
| Builds per identical cold storm | 1 | PASS |
| Peak concurrent frame builds across all scenarios | 1 | PASS |
| Connected bystander during storm | 2 frames in 25s; largest gap 10.026s | PASS, normal 10s push interval |
| Health latency during storm | p50 0.0179s, max 0.0732s | PASS |
| Further 30 storms of 120 clients | zero handlers, connections, flights, active builds after each settle | PASS |
| Retained frames / worker usage | frames stop at 16; 61 threads throughout soak | PASS |
| Soak memory after collection | roughly 221–290 MB; second-half growth 1.6 MB | PASS within tested duration |
| Server ERROR records / frame problems | 0 / 0 | PASS |
| Execution guard | DISABLED at start and finish | PASS |

These are cold panel-cache measurements, not the 0.014s retained-frame path.
The mixed-timeframe scenario explicitly starts a new probe at a cursor for which
no frame exists. OS filesystem caches were not flushed. Resource measurements
include the tool's client sockets in the same process and use its explicit GC
before settled samples; this is a bounded 30-storm observation, not a long-term
memory guarantee. Full samples: `artifacts/independent-ws-storm/results.json`;
complete console output including warnings: `storm-run.log`.

## Correctness review and regression evidence

The lifecycle changes correctly distinguish queued work (cancel when its last
waiter leaves) from running work (drain and observe). They now disconnect only
after the worker starts, and retain explicit no-send, completion and no-task
assertions. The original independent test had become vacuous because its bare
object service raised before any build. It now constructs isolated test-only
Parquet candles and a real service, waits for actual `_build_frame` entry,
disconnects, completes real DuckDB/panel generation and asserts zero sends,
one populated frame, and zero flights.

Existing shared-frame tests cover separate service identity, cursor, timeframe,
replay/feed mode and live-write revision keys, pinning every panel to a cursor,
shared failure/retry, queue cancellation, retention limits and refreshed labels.
STALE, CLOSED, replay pinning/unpinning and feed disconnect cases confirm that
retained panels do not carry old LIVE labels. Real-store storm frames all
retained REPLAY/RECORDED_STORE/is_live=false with matching panel cursors and no
points after their cursor. Completed-candle regressions cover close-time bounds.
An independent deterministic feed-write probe found that the revision key does
not isolate the actual panel reads; reproduction and correction are below.

Executed regressions:

- Focused lifecycle/shared-frame/live-isolation/live-feed/first-run: **144 passed**.
- All backend Market/identity/lifecycle regressions: **306 passed, 1595 deselected**.
- Root tests: **76 passed, 24 skipped, 2 known-defect xfailed** (final rerun).
- Independent HTTP integration, real Uvicorn port 8783: **15/15 passed**.
- API-only Market acceptance: **16/16 passed**.
- Ruff on the changed independent tests and `git diff --check`: passed.
- No Tauri build or native desktop run was performed for this backend-only task.

## Blocking defect: standalone import serves obsolete replay

Exact automated reproduction (isolated generated test fixtures only):

```powershell
python -m pytest tests/integration/test_market_shared_frame_external_import.py --runxfail -q
```

The test performs a genuine `python -m market.ingest --symbols EURUSD
--store-root <temp>` subprocess import, starts real HTTP/WebSocket routes over
that store, receives and retains an H1 frame, corrects the source CSVs, and runs
the standalone importer again while the service remains alive. HTTP `/xray`
reads the new prices; a newly connected WebSocket returns the old prices.
The import and setup assertions pass; the final equality assertion fails with
`key unchanged=True`. Example old EURUSD current_close: 1.10029; corrected price
is 0.1 higher. Complete failure: `external-import-repro.log`.

Cause: `CandleStore.data_revision` counts only in-process `put_live_bars` calls.
Standalone Parquet/manifest replacement neither changes that revision nor
replaces the service. Cache entries have no TTL and can survive indefinitely
at a revisited cursor until eviction/restart. Service bounds/manifest also
remain their construction-time values. The in-app import completion callback
does replace the service, so its tested path does not exhibit this cache-key
failure. External reimports can also create inconsistent old WS/new HTTP panels.

**Claude correction:** make successful standalone store generations observable
to the running backend and invalidate/reload the service and retained frames
consistently, or explicitly prevent such imports while the backend is using
the store. Keep reads and cache identities consistent during publication.
Remove the strict expected-failure marker only once this regression passes;
rerun cold storm, lifecycle, replay and provenance checks. No backend fix was
made outside Codex ownership.

## Blocking defect: a single frame mixes live-data revisions

```powershell
python -m pytest tests/integration/test_market_shared_frame_external_import.py -k feed_revision --runxfail -q
```

The fixture supplies real in-memory live bars through `put_live_bars`. It wraps
the real X-Ray call solely to schedule a feed correction immediately after that
panel returns, before the real strength/leaders calls run. All calculations use
production code. The resulting frame reports EURUSD X-Ray current_close=1.104
and leaders close=1.5 at the same cursor. Captured data_revision=5, current=6.
Complete assertion failure: `feed-revision-repro.log`. Generated candles exist
only in temporary test stores; no actual historical store was modified.

Cause: `FrameInputs` captures a cursor/revision, but the store remains mutable
and `_build_frame` never checks that the revision stayed stable across reads.
The existing test proves that a completed live write changes the *next key*;
it does not prove consistency of a build already running during that write.

**Claude correction:** prevent publishing/retaining a frame whose panel reads
span store revisions, using a focused consistent-read or revision-validation
approach. Add this deterministic interleaving to backend coverage, preserving
queue bounds, cancellation, replay safety and cold-storm latency. Codex has
recorded the confirmed regression as strict xfail pending the backend fix.

## Non-default first-frame behavior

Real Uvicorn clients sent `subscribe` immediately after connect:

| Requested | First frame | Next frame | Time to next |
| --- | --- | --- | --- |
| M5 | H1 | M5 | 12.490s |
| H4 | H1 | H4 | 10.728s |
| D1 | H1 | D1 | 10.625s |

Evidence: `subscription-probe.log`. The sender selects default FrameInputs
before the receiver processes the subscription; the push timer then delays the
requested frame. Thus requested-timeframe-first currently **FAILS**. This is
pre-existing behavior, not a new mixing error: every returned frame truthfully
declares its own timeframe. Claude can add an explicit optional initial
timeframe (preserving H1 for old clients) or equivalent initial subscription
handling; test both default compatibility and requested first frames. Codex
did not alter the protocol or introduce a speculative scheduling fix.

Real authenticated broker-feed behavior remains outside this test environment;
provider-seam regressions passed. Trading remains DISABLED. Do not merge this
verification branch or the optimization into the RC until the replay-data
defect is corrected and independently rechecked. No main/integration merge.
