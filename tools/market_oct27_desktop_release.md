# October 27 desktop release verification — 2026-11-01

Branch: `release/market-xray-final-rc`, isolated worktree
`C:\TARS-Market-XRay-Release-RC`.

Application/build commit: **`4211f70de1f8027348fcc0f07f8d2ac02ccd08b7`**.
Parents: previous RC `928bee92b3d33f7e909b65d579e27c56d86ea10f` and
Claude's `5f63cec69171127aa9c46c3bb7661625b396ff69`.
The merge includes the separately authorized minimal frontend correction below.
Subsequent verification tools and reporting do not change the built application.
Neither main nor integration/v1 was modified.

## Results

| Gate | Actual result |
|---|---|
| Focused backend | **135 passed**, 20.85s. Replay window, no look-ahead, live isolation, socket lifecycle, revision consistency, Market X-Ray. 362 upstream websocket deprecation warnings. Full backend intentionally not repeated. |
| Frontend | Typecheck passed; **255 tests passed in 33 files**, 70.09s, after the correction. Production frontend and Tauri release builds passed. Existing mock/jsdom/React test warnings remain. |
| Rust | **33 passed, 1 hardware-dependent ignored**. No Rust production changes. |
| Runtime dependencies | **558 native modules** checked; every import resolves within the bundle or Windows. |
| Actual native WebView2 labels | **18/18 labels**, no collisions, no clipped text, **72 successful selections** at actual logical client sizes 1024×680, 1280×720, 1440×800, 1680×931 (930 requested; native DPI rounding). Font 9.5 CSS px at DPI scale 1.25. No page exceptions. These are native resizes, not emulated browser viewports. |
| Native fullscreen/restoration | PASS: native fullscreen true, minimized false, 18 labels; client 1536×864 at 1.25 DPI = **1920×1080 physical**. Restored out of fullscreen successfully. |
| Actual WebView2 replay controls | **12/12 passed**: missing-symbol startup recovery, open October window, Next Candle, Step Back, Seek, Play/Pause, M5/H4/H1 switching, automatic stop at 14:00 UTC, honest unavailable-comparison/provenance messages, no JS exceptions. |
| Independent real-data API | **26/26 passed**; **4,365 OHLC candles** compared to independently aggregated vendor M1 bid/ask CSVs. M5/M15/H1/H4/D1 at 10:00, 10:03 and 14:00 UTC. Every candle closed by its cursor. |
| Vendor binary cross-check | Independently decoded **480 BID/ASK minutes** from October 27's original LZMA BI5 files; CSV OHLC matches. All 240 window minutes present; no invalid OHLC envelopes or crossed quotes. |
| Rewind/provenance | Later dashboard visited before rewind; all 40 visible events closed by the rewound cursor using their own timeframe. Seven payloads name historical Dukascopy, `is_live=false`, observed time no later than cursor. M5/H1/H4 first socket frames honor their requested timeframe. |
| Cross-store journal/source isolation | **FAIL — release blocker.** Real packaged gold-only store returned **39 AUDCHF events** at 2026-01-02 10:00 UTC, all covered by a Dukascopy provenance. Two focused regression cases fail for this reason (different symbol and same-symbol legacy data). |
| Black-box integration | **15/15 passed** against bundled backend with the 18-instrument store. The backend is byte-identical before/after the frontend correction. |
| Final package lifecycle | **9/9 passed** on final binary: proven owned bundled Python, execution disabled, populated H4 replay, owned process-loss recovery **16.21s**, same supervising shell, frames resume, replay, cooperative quit, no orphan listener. |
| Backend runtime logs | Saved gold/universe logs contain **zero** ERROR, traceback, unexpected ASGI or unhandled-task signatures. |
| Installer | **26/26 passed**. Real NSIS install 95s; bundled backend startup 15s; real 18-instrument import 41s. Windows-only PATH, empty store NO_DATA, actual packaged runtime, populated replay, companion coexistence, no writes to installation, no-orphan shutdown, uninstall/data preservation. Installed executable and six critical backend modules match the independently verified/reviewed files. **Not a clean-Windows test.** |

The accepted backend milestone remains the independently accepted publication
stabilization code; this pass adds only Claude's small replay/provenance changes,
which were reviewed and exercised by focused regressions and real-data checks.

## Confirmed integration defect and correction

Reproduction on `5f63cec`: run the 18-instrument store, select CHFJPY, quit,
then start with the XAUUSD-only store using the same WebView2 preferences.
`GET /api/v1/market/dashboard?timeframe=H1&symbol=CHFJPY&strength_window=1D`
returns 404 repeatedly. The first-load/retry path keeps the saved symbol instead
of letting the backend select a covered instrument. The native dashboard cannot
finish loading even while the backend and replay API are healthy.

The user explicitly authorized this frontend ownership exception. The correction
retries the aggregate dashboard once without a symbol **only on a 404 for a
selected symbol**, applies the backend's actual available selection, and persists
it. Both first-load and startup-retry paths use it. Other errors propagate.
Three regression tests cover missing-symbol recovery/persistence, a valid saved
selection, and an unrelated 503. The actual final desktop also recovered from
an explicitly seeded CHFJPY preference against the real XAUUSD-only store.

### Remaining P1: persisted journal crosses dataset boundaries — Claude ownership

Confirmed after the normal store-switch verification, using the **real final
packaged backend** and the genuine copied stores. The shared app database retains
old journal rows. `MarketState.recent_events/event_count/zone_count` filter by
cursor, but have no dataset/source identity. `MarketXRayService.events` then
assigns the active store's provenance to every returned row. Timestamp filtering
prevents future rows but cannot establish that older rows belong to this store.

Reproduction on this environment:

1. Use the 18-instrument store and generate/view AUDCHF history before January 2,
   then quit. Keep the normal persistent Market database, as an upgraded user would.
2. Launch the same package using its XAUUSD-only `data` root.
3. POST `/api/v1/market/replay/seek?to=2026-01-02T10%3A00%3A00%2B00%3A00`.
4. GET `/api/v1/market/events?limit=1000`.
5. Observed: **39 AUDCHF rows**, despite `health.store.symbols=1` and only XAUUSD
   Parquet files; payload provenance says `provider=dukascopy`, reconstructed from
   Dukascopy candles. Saved complete response: `cross-store-events.json`.

Deterministic independent gate:

```powershell
python -m pytest tests/integration/test_market_journal_store_isolation.py -q --tb=short
```

**2 failed**, 2.68s, on the intended assertion: one legacy AUDCHF row or one
legacy XAUUSD row is exposed as current Dukascopy replay. The first draft fixture
omitted advertised timeframes and failed because its store was empty; corrected
fixtures provide all five timeframes and reproduce the real provenance defect.
No xfail or other suppression was added.
The legacy fixture timestamp is inside the new store's date coverage, so a
coverage-start filter alone cannot pass this gate.

Focused correction assigned to **Claude Code (`apps/backend/`)**: bind journal
reads/counts and their provenance to the actual dataset/snapshot identity, so
switching stores/providers cannot claim old incompatible rows. Handle existing
unscoped user history conservatively without deleting the user's database. A
symbol-only filter is insufficient: the second regression uses XAUUSD in both
contexts. Preserve the working cursor boundary and other accepted backend gates.
No backend production fix was made in this verification pass.

No backend production code, contracts, design system, chart prices, or trading
execution behavior was changed by Codex. Claude's backend diff adds optional
recorded vendor provenance and applies the existing closed-candle rule to stored
event reads/counts and zone counts. Old manifests still identify `parquet_store`.
Tests cover rewinding after a later view and equivalence to a store ending at the
cursor. Stored rows are preserved; replay filtering does not delete history.

## Evidence and reproduction

Full logs/JSON/screenshots are under
`C:\TARS-Market-XRay-Release-RC\artifacts\desktop-labels-oct27`.

```powershell
# Backend cwd: apps/backend
python -m pytest tests/test_market_replay_window.py tests/test_market_no_lookahead.py tests/test_market_live_isolation.py tests/test_market_ws_lifecycle.py tests/test_market_revision_consistency.py tests/test_market_xray.py -q --tb=short

# Frontend cwd: apps/web
npm.cmd run typecheck
npm.cmd test -- --reporter=dot
# Tauri release includes a fresh production frontend build.
$env:CARGO_TARGET_DIR='D:\TARS-Release-RC-target'
.\node_modules\.bin\tauri.cmd build --config src-tauri/tauri.bundle.conf.json

# Repository cwd. Test the running package with the supplied copied replay store.
python tools/verify_oct27_replay.py --api http://127.0.0.1:8775 --csv-root D:\TARS_REPLAY_20261027\source_csv --raw-root D:\TARS_REPLAY_20261027\raw --output artifacts/desktop-labels-oct27/replay-api-final.json

# Test-only WebView2 diagnostics: set this before launching the native package.
# The diagnostic listener was confirmed bound to 127.0.0.1, not the network.
$env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS='--remote-debugging-port=8797 --remote-debugging-address=127.0.0.1'
python tools/verify_webview_labels.py --app D:\TARS-Releases\market-xray-oct27-5f63cec\tars-companion.exe --cdp http://127.0.0.1:8797 --output artifacts/desktop-labels-oct27/labels-final.json
python tools/verify_webview_replay.py --app D:\TARS-Releases\market-xray-oct27-5f63cec\tars-companion.exe --cdp http://127.0.0.1:8797 --seed-missing-symbol --output artifacts/desktop-labels-oct27/replay-desktop.json
```

Run labels against `data-universe`, replay controls/API against `data`, quitting
between data-root changes. The two stores are intentionally separate. The
WebView2 tools reject ordinary browser pages and validate the debug listener's
native process ancestry and the exact package executable hash. The original
12-control run preceded adding the explicit ancestry precondition; ancestry was
also independently observed as `msedgewebview2.exe` parented by the exact package.

Native screenshots include the OS window frame:
`native-gold/native-launch.png` and `native-universe/native-launch.png`.
The approved existing dashboard styling/layout was preserved.

Automation problems were separated from application failures: an initial test
command named a nonexistent lifecycle test and ran no tests; the corrected
selection passed 135. One label run targeted the still-closing gold session after
a shell-level quit ended the command before relaunching; its one-label assertion
was a fixture error, and the explicit 18-symbol precondition/final rerun passed.
The generic desktop capture helper also returned an occluded foreground surface;
that capture was discarded. Final evidence uses the exact native PID/window and
the package-owned WebView2 target. The earlier NSIS build was deliberately stopped
to include the authorized frontend fix, then rebuilt.

## Demo and release boundaries

Portable demo: `D:\TARS-Releases\market-xray-oct27-5f63cec`.
Installer: `D:\TARS-Releases\market-xray-oct27-5f63cec\TARS_1.0.0_x64-setup.exe`,
**208,036,799 bytes**; SHA-256
`29EB21BA87436D1A915093AD21078C33DDB5D615F10F0B696BAA9E49FBBC0903`.
Installed/portable executable SHA-256
`C52E9A1D1A9E3A8EF42776CE6CDAEAE6D92B06C78271FDACAF33642F8C17CF80`.
Exact package verification command:

```powershell
python tools/verify_installer.py --installer D:\TARS-Releases\market-xray-oct27-5f63cec\TARS_1.0.0_x64-setup.exe --expected-executable D:\TARS-Releases\market-xray-oct27-5f63cec\tars-companion.exe --expected-backend apps/backend
```

Its README is copied from `tools/market_oct27_demo.md`. Launch using its
`start_offline_demo.ps1`; `data` is the default October replay and
`-DataRoot .\data-universe` selects recorded multi-instrument analytics.
The whole folder is portable. Runtime reads use the copied store's relative
generation, not original absolute CSV/import audit paths retained in its manifest.
The original data folders were not modified. Replay dataset revision:
`0629cb94da264bdea83de23a7928a7f0` (5 Parquet files, 2,140,692 bytes).

Single-instrument currency strength is unavailable. Coverage explicitly warns
that cross-instrument rankings are unavailable; any XAUUSD leader card is relative
only to the one available instrument. The separate recorded universe is 17 FX
pairs plus gold, with existing thin-data warnings. No FX history was fabricated.

Not verified: genuinely clean Windows installation (no provisioned clean Windows
VM/Sandbox available), human tray interaction, authenticated MT5 LIVE/STALE.
Backend restart still resets the replay cursor; Play is paced by completed panel
loads and is not a real-time performance claim. No statistical profitability,
live trades, or real-time broker data is claimed. Execution remains **DISABLED**.

**Recommendation: NO-GO for unconditional release.** The confirmed journal
dataset/provenance defect must be corrected before asserting full replay/source
integrity. Native charts, labels and the requested October 27 controls work and
have demonstration screenshots, but journal/event counts must not be presented
as dataset-specific evidence. Also retain the explicit clean-Windows, human-tray
and authenticated-feed verification gaps. Keep the follow-up limited to this
confirmed backend correction and its regressions; no new feature phase is needed.
