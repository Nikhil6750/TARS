# Independent backend acceptance — 2026-11-01

**PASS for the defined backend stabilization milestone.** No remaining
release-blocking defect was found in the tested workload. Freeze backend scope
and proceed to release-candidate integration; this is not final installer or
authenticated-live-feed certification.

Reviewed `fix/market-publication-stabilization` at
`1ed0388b1ecfa8a73eb7cd19f8011d11231ce340`; measured code commit
`cfc529ae894210fabc9148019ff8743030123b51`. The intervening diff contains
documentation only. Independent branch: `verify/backend-final-acceptance`;
worktree: `C:\TARS-Backend-Final-Acceptance`. No production edits or Tauri build.

## Focused acceptance evidence

| Requirement | Result and scope |
| --- | --- |
| Monitor cancellation racing HTTP timeout | PASS: deterministic real anyio timeout-scope race, provider client closure, restart, ordinary failure/retry and manager cleanup tests |
| Bounded monitor cleanup | PASS for configured HTTP adapters and the tested race; 5s per stuck monitor, one extra cancel and explicit error log; abnormal-provider limitation below |
| Windows manifest sharing | PASS: transient WinError 5/32/33 and bare EACCES retry, bounded persistent refusal, unrelated error propagation, real multi-process reader/publisher contention |
| Corrupt manifests | PASS: malformed JSON, empty/non-object, incomplete generation/revision pair fail closed; no silent flat-store fallback; legacy stores remain compatible |
| Earlier three defects | PASS: failed target retains prior data; concurrent target publications preserve both commits; cancelling initial WS wait leaves no receiver |
| Standard 120-client storm | PASS: probe 0.12794s; cold normal first connection 2.44211s |
| Harder cold storm, abort delay 0.15s | PASS: 1.97403s and 0.79309s |
| Harder mixed-timeframe cold probes | PASS: 0.94949s and 1.00963s |
| Socket/snapshot/provenance guards | PASS: zero server errors or frame problems in real-store storms; independent original defects and focused completed-candle/live/replay tests pass |
| Trading execution | DISABLED throughout real Uvicorn performance verification |

Executed focused backend command (88 passed, 19.74s):

```powershell
python -u -m pytest tests/test_monitor_shutdown.py tests/test_market_publication.py tests/test_market_revision_consistency.py tests/test_market_live_isolation.py -q
```

Independent original regressions (7 passed, 10.17s, no xfail):

```powershell
python -u -m pytest tests/integration/test_market_generation_publication.py tests/integration/test_market_ws_initial_cancel.py tests/integration/test_market_shared_frame_external_import.py tests/integration/test_market_ws_disconnect.py --runxfail -q
```

Focused existing WS lifecycle/shared-frame suite: **75 passed**, 38.15s. The
previous three timing failures and service-creation stub thread errors no
longer occur. These tests now wait for actual frames/builds, rather than
disconnecting entirely within the initial subscription wait. The independent
concurrency barrier change is justified: correct publication serialization
prevents both publishers reaching rename together; its bounded wait still
forces the overlap on the old unlocked implementation.

## Real Uvicorn / historical-store performance

```powershell
python -u tools/measure_market_ws_reconnect.py --storm-clients 120 --port 8781 --timeout 120 --artifact-dir artifacts/standard-storm
python -u tools/measure_market_ws_storm.py --port 8782 --clients 120 --abort-delay 0.15 --repeats 2 --scenarios cold timeframes --report artifacts/harder-storm/results.json
```

Run sequentially before the complete backend suite, against the actual app and
real 18-instrument store. No user history was rewritten. Cold means a new frame
cache key/cursor; OS filesystem cache was not flushed. The mixed scenario moves
the cursor again after the abort storm, so its probe cannot use retained panels
from that storm. All measured first frames remain below 5s.

Standard run settled with zero connections, handler tasks and active builds;
one started/finished build, peak one. Harder run: 14 started/finished builds,
zero active at completion, peak one. Frames report REPLAY/RECORDED_STORE and
is_live=false, matching panel cursors; frame checks found no look-ahead or
timeframe mismatch. Actual output and samples:
`standard-storm.log`, `artifacts/standard-storm/reconnect-latency.json`,
`artifacts/standard-storm/uvicorn.log`, `harder-storm.log`,
`artifacts/harder-storm/results.json`.

## Bounded cleanup: what it guarantees

The stopping flag is set before cancellation. When httpx/anyio turns a racing
cancel into ReadTimeout, the monitor returns instead of entering its next sleep.
The deterministic scope test covers this actual mechanism; client closure is
also asserted. Those tested tasks finish, so they cannot touch a closed DB.

The defensive helper does **not** forcibly terminate arbitrary cancellation-
resistant tasks. After 5s it logs, cancels once more, and returns. Such a task can
outlive `db.close()`, call its callbacks later, and delay final event-loop/process
cleanup if it also ignores subsequent cancellation. Existing stubborn-provider
tests deliberately demonstrate that residual task and then release it. Thus the
bound is on the monitor stop helper, not a universal bound on process exit for
uncooperative custom providers. No shipped HTTP provider is shown doing this;
the previously reproduced real HTTP-timeout race is fixed. Retain this as an
exceptional-provider limitation, not an assertion that abandoned tasks are safe.

## Four continuously running imports

Claude's reported 39.5s remains a documented overload limitation, **not a new
acceptance measurement here**. No exploratory four-import sweep was repeated.
Data correctness supports concurrent publishers via the SQLite lock, but the
ordinary dashboard's `ImportJob.start()` admits only one import at a time.
Four external import processes continuously replacing generations are outside
that interactive workload: they invalidate pending frames, consume the shared
store/CPU and can exhaust bounded frame retries. The <5s reconnect acceptance
target is met without that continuous bulk-publication load; it is not met or
promised during it. This does not block the defined backend milestone. Preserve
the limitation in RC release notes and schedule bulk reimports separately from
latency-sensitive viewing. If continuous parallel ingestion becomes a supported
interactive requirement, it needs its own performance target and work item.

Other retained limitations: generations have no automatic reclamation; persistent
sharing refusals fail safely after the retry bound; corrupt-manifest recovery
requires a clean full CLI import because setup/import routes also fail closed;
WS replay control can synchronously wait up to the read retry budget (0.25s,
plus scheduling/I/O). Authenticated MT5 and clean Windows installation remain
separate RC verification gates; this backend review does not claim them.

## Complete backend run and recommendation

One complete run was started after the storm/focused correctness gates:

```powershell
python -u -m pytest -q --durations=15 -o faulthandler_timeout=240
```

**1,953 passed, 8 skipped, zero failures**, exit code 0, **802.57 seconds**
(13m22s). No shutdown hang recurred. Evidence: `complete-backend.log`;
no overlapping complete-suite run was launched. The log retains 754 warnings,
including WebSocket/TestClient deprecations and existing response-stream mock
cleanup warnings; no failing acceptance gate was hidden by them.

Focused evidence: `focused-acceptance.log`, `independent-regressions.log`,
`focused-websocket.log`. Only this report and the Codex handoff were edited.

**Recommendation:** integrate `fix/market-publication-stabilization` at
`1ed0388b1ecfa8a73eb7cd19f8011d11231ce340` into the release candidate, preserving
the measured code `cfc529ae894210fabc9148019ff8743030123b51`. Freeze backend
development for this milestone. Retain the workload/abandoned-task limitations
above; resolve separate installer and real-feed gates in RC verification.
No integration or main merge was performed by this review.
