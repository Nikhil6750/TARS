# Final desktop release candidate and offline demo — 2026-11-01

Branch: `release/market-xray-final-rc`, isolated worktree
`C:\TARS-Market-XRay-Release-RC`. Accepted backend
`1ed0388b1ecfa8a73eb7cd19f8011d11231ce340` integrated by merge `8572f58`;
independent acceptance `3b747dd` integrated by merge `edca51f`.
Desktop/installer build source: `cb83b6af20a73cd049280c74bb79433cb6eb5db2`.
Later changes are verification/reporting tools only. Approved dashboard source
and contracts remain unchanged. No main or integration/v1 merge.

## Verification

| Check | Actual result |
| --- | --- |
| Frontend typecheck | Passed |
| Frontend production build | Passed |
| Frontend Vitest | 217 passed across 31 files |
| Rust unit tests | 33 passed, 1 hardware-only ignored |
| Integrated root contracts/regressions | 82 passed, 24 skipped |
| Native release build | Passed, production assets embedded; 4m11s |
| Bundled runtime dependency scan | 558 modules; no unresolved non-Windows imports |
| Packaged HTTP integration | 15/15 passed |
| Production web assets + packaged backend, Chromium | 27/27 passed |
| Non-interactive packaged lifecycle/recovery | 9/9 passed |
| Offline-mode rendered data and replay | Passed with external browser requests blocked |
| UI reconnect without reload | Passed: 22.01s after proven-owned backend loss, 8 currency rows and no error panel |
| Exact NSIS artifact, isolated developer-host install/uninstall | 24/24 passed, zero failures |

The packaged backend runs on its own `tars/python/python.exe`, not the system
Python, and is an owned child of the packaged desktop. Forced loss recovered
in 15.32s with a new instance/PID; H4 WebSocket frames resumed. Replay stepped
correctly. Cooperative `--quit` stopped the shell and left no backend listener.
Runtime copies of router, frames, ingest, store and both monitor modules hash
identically to the integrated accepted source. The already accepted unchanged
backend code has independent complete-suite evidence: 1,953 passed / 8 skipped
at `3b747dd`; that long suite was not unnecessarily repeated for packaging.

Native computer-use inspection observed the actual `tauri.localhost` dashboard,
populated X-Ray/candles/rankings, `ws connected`, RECORDED STORE / REPLAY and
execution DISABLED. The user pressed physical Escape, stopping Computer Use.
No subsequent mouse/keyboard automation was issued. Further F11/resize/manual
native interaction was not verified in this release run. Headless frontend and
process/API checks above were performed independently of native input.

Evidence in this worktree: `desktop-installer-build.log`, `runtime-build.log`,
`bundle-dependencies.log`, `desktop-root-regression.log`, `final-rc-browser.log`,
`artifacts/final-rc-api.json`, `final-packaged-lifecycle.log`,
`artifacts/final-packaged-lifecycle.json`, `offline-demo-verification.log`,
`ui-recovery.log`, and screenshots under `artifacts/market-xray/`.

## Offline demo

Ready-to-run folder: `D:\TARS-Releases\market-xray-rc-cb83b6a`.
It contains the production desktop, bundled Python/dependencies/backend and a
separate copy of genuine recorded market data. It needs no developer checkout,
Node, Rust, Vite, API key or installed Python. The normal app does not bundle
user history in its installer; this demo's data is supplied separately.
Windows WebView2 is required and present on the tested host; a new clean
machine's runtime availability remains part of the unverified install gate.

Launch from PowerShell:

```powershell
& 'D:\TARS-Releases\market-xray-rc-cb83b6a\start_offline_demo.ps1'
```

Cooperative shutdown:

```powershell
& 'D:\TARS-Releases\market-xray-rc-cb83b6a\start_offline_demo.ps1' -Quit
```

The launcher uses port 8775, refuses to adopt an occupied port or another TARS
copy, and disables MT5/news/calendar/external monitors and mock trading events.
Assistant and voice providers are inert mocks for this market-only offline demo;
the market panels use real recorded candles, not mock market data. It never
builds, downloads or imports history on launch. The data copy is separate from
`D:\TARS_DATA`; the original recorded store was not rewritten.

Suggested demo: show the REPLAY/HISTORICAL banner, eight currency rankings and
their coverage warnings; hover X-Ray; click a bullish/bearish instrument; switch
H1/H4; step back 12 bars and return to latest. Tested offline-render results:
8 currency rows, 12 leader cards, cursor 2026-09-11 21:00 → 09:00 UTC. The
production UI recovered after backend loss without reloading the page.

Coverage is 18 instruments (17 FX + XAUUSD); NZD/CHF thin-coverage warnings are
retained. This is recorded history, not authenticated broker data or a claim of
the broader 26-instrument target. X-Ray measures price movement; descriptive
zone rankings are not win probabilities. No profitability, live-trade or
real-time broker claim is made. Trading execution remains DISABLED.

## Installer and release gates

Build command used:

```powershell
$env:CARGO_TARGET_DIR='D:\TARS-Release-RC-target'
# From apps/web, after scripts/build-backend-runtime.py completed:
.\node_modules\.bin\tauri.cmd build --config src-tauri/tauri.bundle.conf.json
```

The explicit bundle config includes the accepted backend runtime. Installer:
`D:\TARS-Releases\market-xray-rc-cb83b6a\TARS_1.0.0_x64-setup.exe`.
Original build output:
`D:\TARS-Release-RC-target\release\bundle\nsis\TARS_1.0.0_x64-setup.exe`.
Size: **208,033,181 bytes**. SHA-256:
`B13F982B1C370F2336BA47E8DB9322EE4395328CFC1006FBC717B0C3FCCA4AAB`.

Exact-artifact verification:

```powershell
python -u tools/verify_installer.py --installer D:\TARS-Release-RC-target\release\bundle\nsis\TARS_1.0.0_x64-setup.exe
```

**24 passed, zero failed**, exit 0. PATH was restricted to Windows, with no
developer variables and an empty market-data root. Installation exited 0 in
98s; bundled backend started in 35s; NO_DATA onboarding was correct; the in-app
import loaded 18 genuine instruments in 66s; populated offline panels and replay
worked; companion and Market coexisted; cooperative quit left no listener.
No user data/secrets were packaged and no file was written into the installation
directory. Uninstall passed; user data survived. The generated temporary test
installation/data copy was removed; it is reproducible from the retained
installer. Evidence: `final-installer-verification.log` and
`artifacts/market-desktop/installer-verification.json`.

Portable demo executable matches the executable extracted from this installer
byte-for-byte, SHA-256:
`531E011A1218DEE145C8934BAA77685AF006CE8F4C3EC0F7523C11EBAB0B2ABC`.
Tauri changes its bundle-type stamp while packaging, so this comparison uses the
installed executable rather than the subsequently unstamped raw Cargo output.

No genuinely clean Windows environment was available in this session: this
host has developer dependencies, no WindowsSandbox.exe, and no provisioned
clean VM accessible to the task. An isolated developer-host installation does
not close that gate. Human tray interaction is unverified. MT5 LIVE/STALE is
unverified until actual broker authentication and suitable market conditions.

## Decision

**GO for the recorded-data offline demo and RC handoff. NO-GO for an unrestricted
public release certification** while clean-Windows installation and human tray
interaction remain unverified. Native input automation was stopped with Escape;
no current fullscreen/resize/manual-interaction certification is claimed.
MT5 LIVE/STALE remains explicitly unverified, not a prerequisite for truthful
recorded-history demo operation. The earlier 26-instrument target is not met by
this 18-instrument artifact; coverage warnings and that limitation are preserved.
No new code defect was found in the executed release checks.
