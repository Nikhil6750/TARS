# Market X-Ray integrated release candidate — 2026-11-01

Recommendation: **NO-GO**. The WebSocket close race is fixed, but reconnect
storms leave enough frame work queued to delay a new client's first frame by
106.086 seconds. Backend ownership must address this measured latency defect.

Branch: `release/market-xray-final-rc`, isolated worktree
`C:\TARS-Market-XRay-Release-RC`.

Reviewed application merge: `9030b789ba61` (base `7fe5875`, WebSocket source
`5183708`, verification source `d6d57fb`). Explicit merge commits are `d88ba08`
and `9030b78`. Verification implementation: `cb72d21efee0ec3155ec8938d9e8c32d3372b52d`.
No main/integration merge, UI redesign, or backend optimization was performed.

## Executed verification

| Gate | Result |
| --- | --- |
| Complete backend suite | 1,846 passed, 8 skipped, exit 0, 1,062.62 seconds |
| Root contract/integration suite | 76 passed, 24 skipped |
| Focused disconnect, empty-store and live-isolation rerun | 42 passed |
| Backend WebSocket lifecycle tests | 27 passed |
| Independent disconnect regression | XPASS before removal; PASS after removal |
| Frontend Vitest | 217 passed across 31 files |
| Frontend typecheck / production build | Passed |
| Frontend lint | Failed: existing prefer-const in src/test/orb-native.test.ts:4 |
| Rust tests | 33 passed, 1 hardware capture test ignored |
| Fresh Rust release build | Passed in 6m57s |
| Independent HTTP API | 15/15 passed |
| API + Chromium | 27/27 passed on three consecutive runs |
| Native ownership/port/recovery/single-instance matrix | 44 passed, 0 failed, 0 unverified |
| Production-asset native desktop | 19/19 passed; 0 failed, 0 unverified; cooperative quit left no owned backend |
| Ruff / Python compile / diff whitespace | Passed for changed verification files |

Backend invocation: `python -m pytest -q --durations=20 -o faulthandler_timeout=180`
from `apps/backend`. The watchdog exposed the real XAUUSD quality test's calendar
path at 180 seconds; that test completed in 219.67 seconds. An earlier full run
completed all cases but exited 1 because the JUnit reporter could not write to
`C:\TARS-RC-backend-junit.xml`; it is excluded from the green result. Initial
load-contaminated runs were interrupted and likewise excluded.

Chromium ran against Vite explicitly bound to `127.0.0.1:5173` and real Uvicorn
on `127.0.0.1:8000`. The reported Chromium timeout did not recur. Complete output
of repeat runs is retained in `artifacts/market-rc-chromium-repeat.log` and
`artifacts/market-rc-chromium-third.log`; screenshots are under
`artifacts/market-xray/`. API evidence: `artifacts/market-rc-api.json`.

Ownership evidence: `artifacts/market-desktop/ownership-verification.json`.
An initial visual verifier attempt exited with Win32 error 1400 (invalid HWND)
during capture. Reopening through `--market` worked. CUA independently found
the reopened window serving WebView's localhost connection-refused page: plain
`cargo build --release` uses development asset mode in this project. A native
production visual check requires `cargo build --release --features tauri/custom-protocol`.
This failed setup attempt is retained in `artifacts/market-rc-desktop.log`.
The corrected production-asset build completed in 2m50s. Its verifier passed all
19 checks, including exact 1920x1080 fullscreen and restoration to
(48,11)-(1871,1017), resize, companion survival after closing Market, reopen,
and no-orphan cooperative quit. Complete output is in
`artifacts/market-rc-desktop-production.log`; structured evidence and native
screenshots are under `artifacts/market-desktop/`. The ownership matrix used
the plain Cargo binary; the supervisor source is identical in both asset modes.
The intermittent minimized launch did not reproduce.

Replay/live-provider isolation, completed-candle bounds, historical non-LIVE
labels, STALE/CLOSED is_live=false, timestamps and provenance passed backend
regressions. HTTP and browser observations remained REPLAY/RECORDED_STORE.
The API repeatedly returned trading_execution=DISABLED. Empty-store tests use
real empty directories; tiny generated fixtures are test inputs only and are
never added to product history. Required universe coverage remains 18 instruments
(17 FX plus gold), with warnings retained.

## Confirmed reconnect defect: reproduction and evidence

From this worktree, with real recorded store available at the configured data root:

```powershell
python tools/measure_market_ws_reconnect.py --storm-clients 120 --port 8781 --timeout 300 --interactive-target 5
```

This starts the actual application under Uvicorn. Instrumentation only wraps
`_build_frame` to count and time calls; frame results/scheduling are unchanged.
The tool measures three normal connections, two immediate reconnects, then
120 simultaneous connections aborted after 20ms and an immediate fresh probe.
It exits 1 because the declared 5-second interactive target is exceeded.

| Measurement | Observed |
| --- | --- |
| Normal first-frame times | 1.9264, 0.7801, 0.9429 seconds |
| Immediate reconnect pair | 0.9635, 1.0220 seconds |
| First frame after 5-client storm | 5.3564 seconds |
| First frame after 120-client storm | 106.0860 seconds |
| Peak concurrent builds | 16 |
| Started / finished builds after settling | 126 / 126 |
| Mean / max build duration | 13.1331 / 14.5963 seconds |
| Storm connection errors / server ERROR records | 0 / 0 |

At the fresh probe: 120 handler tasks, 16 active builds, 21 builds started and
5 finished. At about 75 seconds only the probe handler remained, while 16
background builds still ran (101 started, 85 finished). At about 90 seconds
117 had started and 101 finished. The fresh frame arrived only after draining
the backlog. All work eventually finished; this is severe temporary starvation,
not an infinite leak. There were 56 warnings that a build was still running
after the 60-second drain deadline. No `Unexpected ASGI`, traceback, or server
ERROR occurred. The returned frame retained REPLAY/RECORDED_STORE/is_live=false.

Full samples and logs: `artifacts/market-ws-performance/reconnect-latency.json`
and `artifacts/market-ws-performance/uvicorn.log`. The first captured JSON has
trading_execution=null because the original diagnostic queried general health;
the committed tool queries market health correctly. Execution-disabled evidence
comes from the separate HTTP verification, not that null field.

Claude Code dependency: bound/cancel or otherwise avoid abandoned frame work
starving an active client, preserving replay accuracy and the new socket safety.
The current implementation meets the 5-second target normally but fails it
after a reconnect storm. No speculative backend fix was applied from Codex's lane.

## Remaining limitations

- Real authenticated MT5 LIVE/STALE behavior is blocked: this terminal reports
  AUTH_REQUIRED. Synthetic-provider regressions passed; these do not establish
  real broker behavior.
- Clean Windows installation is blocked: no Windows Sandbox executable or
  usable clean VM was identified. The developer-host installer validation from
  the previous review is not substituted for this gate. The existing installer
  is 208,044,916 bytes, SHA-256
  `0574CA45D1688A8C872537644604DBB4F5569241567FE9630F333CA259B9203D`.
  Its previously verified bundled runtime does not establish that it packages
  this newly integrated WebSocket fix; rebuild and validate the final installer
  after resolving the release blocker.
- Eight target FX pairs remain absent: USDCHF, EURCHF, EURNZD, GBPCHF, AUDNZD,
  NZDJPY, NZDCAD, CADJPY. The existing registry/coverage-note discrepancy remains
  assigned to backend ownership; no history or coverage was fabricated.
- npm install audit reported 28 frontend dependency vulnerabilities (2 critical,
  5 high, 18 moderate, 3 low) and one high in codegen. No dependency upgrade or
  exploit assessment was part of this integration.

The earlier send-after-close finding is resolved in this application RC. The
remaining reconnect latency, coverage requirement, lint failure, and blocked
real-world gates prevent a release recommendation.
