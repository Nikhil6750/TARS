# Market X-Ray

Deterministic Forex and XAUUSD market intelligence inside TARS. Every number
it shows is computed from recorded bid/ask candles or a logged-in MetaTrader 5
terminal. It places no trades, needs no API key, and never labels recorded
history as live.

## Running it

**Desktop (the intended shell).** Opens the dashboard as a native window of
the existing Tauri app:

```
scripts\start-market-desktop.cmd
```

On first run this builds the release binary (several minutes) and then
launches it; afterwards it starts immediately. The Tauri shell's own
`BackendSupervisor` starts and supervises the Python backend, so nothing else
needs to be running.

Building needs the MSVC C++ build tools, because Tauri compiles a native
binary. The script checks for them first and prints exactly what to install
if they are missing.

Other ways in and out, once TARS is running:

```
tars-companion.exe --market                 # open (or focus) Market Intelligence
tars-companion.exe --quit                   # cooperative stop, backend included
scripts\start-market-desktop.cmd -Dev       # Vite dev server + debug binary
scripts\start-market-desktop.cmd -Rebuild   # force a fresh release build
```

The window is also in the tray menu as **Market Intelligence**. A second
launch never starts a second copy: the single-instance plugin routes it to the
running app.

`--market` works from cold as well as on a running app, and `--quit` with
nothing running exits without starting anything.

**Installer.** `scripts\build-installer.cmd` produces
`apps/web/src-tauri/target/release/bundle/nsis/TARS_1.0.0_x64-setup.exe`, about
210 MB. It carries its own Python, dependencies, Visual C++ runtime and backend,
so the machine it lands on needs none of them. A plain `npm run tauri build`
produces an installer with no backend in it. Writable state goes to
`%LOCALAPPDATA%\com.tars.companion`, which an upgrade or uninstall leaves alone,
and the backend logs to `logs\backend.log` there.

**First run.** With an empty store the window opens on a first-run screen that
says what is missing and imports from a folder you choose. Nothing has to be run
in a terminal, and no data is invented while the store is empty.

**Window behaviour.** An ordinary resizable desktop window with native
decorations and a taskbar entry, minimum 1024x680. F11 toggles fullscreen.
Closing it closes only it — the companion and the backend keep running — and
it reopens from the tray or `--market`.

**Browser (development and verification).** Same dashboard, same backend,
served by Vite:

```
scripts\start-market-xray.cmd
```

Opens <http://127.0.0.1:5173/market.html>. The companion app stays at `/`.

## Building the candle store

Both launchers build it automatically on first run. To do it by hand:

```
cd apps\backend
python -m market.ingest                       # all 18 instruments, ~35s
python -m market.ingest --symbols EURUSD XAUUSD
python -m market.ingest --start 2022-01-01    # a longer window
```

Neither location is hardcoded. The store goes under the repo's shared data
root, resolved by `research/config.py` — `TARS_DATA_ROOT` if set, else a
non-system drive with room to spare, else a repo-local fallback. On this
machine that is `D:\TARS_DATA\market\xray_store`. Source CSVs are read from
`C:\Trade Data` unless `TARS_MARKET_CSV_ROOT` says otherwise.

| Variable | Effect |
|---|---|
| `TARS_DATA_ROOT` | Moves the candle store, and every other TARS dataset. |
| `TARS_MARKET_CSV_ROOT` | Where the source bid/ask CSVs are read from. |

The ingester only ever writes under the store root — quant_brain's frozen
research partitions (`<data root>\market\local_xauusd`, `...\dukascopy`) are
never modified.

## What the numbers mean

**Market X-Ray.** Each curve is one instrument's volatility-normalised return
(`z`) at every completed bar in the window: the move over a trailing window
divided by that instrument's own typical movement over the same horizon.
Dividing by each instrument's own volatility is what lets gold and EURUSD
share one axis. Above the zero line is a rise over the window, below is a
fall.

This is **price displacement**. It is not institutional money flow, not order
flow, and not a forecast — nothing in the pipeline observes who traded or why.

**Currency strength.** For each pair, the signed normalised return is
attributed to both legs (`+z` to the base, `−z` to the quote); a currency's
score is the mean of its contributions. Because the pair set here is uneven,
every score carries its coverage and a leave-one-pair-out diagnostic showing
how far it moves if any single pair is dropped. NZD rests on one pair and CHF
on two; both are flagged `thin_coverage` and badged in the UI.

**Order blocks.** A bullish zone requires all three, in order:

1. a displacement candle — an up-close candle whose body is at least
   `displacement_atr` × Wilder ATR(14) at that bar;
2. an origin candle — the most recent down-close candle before it, within
   `origin_lookback` bars, whose full high–low range becomes the zone;
3. a break of structure — the displacement candle must close above the most
   recent confirmed swing high formed before the origin candle.

Bearish zones are the exact mirror. Zones move through
`FRESH → TAPPED → MITIGATED`, and terminate at `INVALIDATED` (a close beyond
the far side) or `EXPIRED`.

The `score` is a **descriptive ranking** of how cleanly a zone met these
rules. It is not a win rate and not a probability.

## Feed status

`LIVE` describes the panels, so it takes more than a fresh tick. All of these must
hold: a real feed, a tick that changed within the freshness window, the market
open, no replay pin, and the panels computed from that feed's own bars.

| Status | Meaning |
|---|---|
| `LIVE` | All of the above. |
| `STALE` | The panels come from the feed, but its last tick has aged out or it has disconnected. |
| `REPLAY` | Recorded history. Always says how far behind it is. A connected feed that has not delivered bars yet also reads `REPLAY`, because that is what is on screen. |
| `CLOSED` | Outside trading hours. |
| `NO_DATA` | Nothing to show yet. |

`status.feed_connection` says why, separately from the status: `NOT_CONFIGURED`,
`AUTH_REQUIRED` (a terminal is running with no account logged in), `DISCONNECTED`
or `CONNECTED`. Every payload's `provenance.is_live` is true only under `LIVE`.

### MetaTrader 5

TARS attaches read-only to a terminal you have already logged in to. It never asks
for, reads or stores broker credentials, and has no order path. If your broker
adds a suffix to symbol names, set `TARS_MT5_SYMBOL_SUFFIX`.

MT5 stamps ticks in broker server time, so freshness is measured by when TARS saw a
tick change, not by the broker's clock, and an unchanged quote never counts: a
terminal that has lost its server keeps returning its last tick. The broker's offset
from UTC is estimated from those changing ticks and used to place bars. The rate
still forming is dropped, and H4 and D1 are rebuilt from H1 on UTC boundaries,
because the broker cuts its own on server time.

Live bars are held in memory beside the recorded store and only read after recorded
history ends, so a feed cannot alter the record, and a replay is always a replay of
what was recorded.

**Not yet verified against a real broker.** This machine has no logged-in account.
The state machine is tested through the real provider against a fake terminal, which
proves the logic and nothing about a real session.

## Replay

The cursor lives on the backend, so every client sees the same moment. A bar is
visible only once it has closed: a bar's `ts` is its open, so reads stop at
`ts + bar length <= cursor`, in SQL rather than trimmed afterwards. Rewinding
therefore cannot reveal a bar still forming at that moment.
Stepping back to a previous moment reproduces that moment's payload exactly.

```
POST /api/v1/market/replay/step?timeframe=H1&bars=-12
POST /api/v1/market/replay/seek?to=2026-06-02T12:00:00+00:00
POST /api/v1/market/replay/reset
```

## API

| Endpoint | Returns |
|---|---|
| `GET /api/v1/market/health` | Feed status, store coverage, persisted counts |
| `GET /api/v1/market/instruments` | The data-backed universe |
| `GET /api/v1/market/dashboard` | Everything needed for a first paint |
| `GET /api/v1/market/xray` | X-Ray series |
| `GET /api/v1/market/strength` | 8-currency ranking with coverage |
| `GET /api/v1/market/leaders` | Bullish/bearish boards |
| `GET /api/v1/market/candles/{symbol}` | Completed candles + regime |
| `GET /api/v1/market/order-blocks/{symbol}` | Zones, lifecycle and events |
| `GET /api/v1/market/events` | Persisted zone-interaction history |
| `GET /api/v1/market/setup` | What exists, for the first-run screen. Answers with an empty store |
| `POST /api/v1/market/import` | Build the store from a local folder, in the background |
| `WS /api/v1/market/ws` | `market.frame` every 10s. Closes 1008 on a message it cannot read, 1011 on an internal error |

Separately, `GET /api/v1/health` carries an `identity` block: `instance_id`
(this process), `build_id` and `build_source` (this code — the commit SHA
where the repository is readable, else the release version), `app_version`
and `started_at`. The desktop shell adopts whatever answers that endpoint, so
these are what let it tell the backend it expects from an orphan left behind
by an older build. `TARS_BUILD_ID` overrides the build id, for packaging to
stamp what it produced.

Compatibility and ownership are separate. `service` and `contract_version` decide
whether the shell may talk to a backend; `owner_id` decides whether it may stop
one. A compatible backend started elsewhere is used and left running, and anything
else holding the port is reported and never touched, even though that leaves the
app degraded. `TARS_BACKEND_URL` moves the shell, the backend and the webviews
together, and accepts loopback addresses only.

## Coverage

18 instruments have recorded bid/ask data here: AUDCAD, AUDCHF, AUDJPY,
AUDUSD, CHFJPY, EURAUD, EURCAD, EURGBP, EURJPY, EURUSD, GBPAUD, GBPCAD,
GBPJPY, GBPUSD, NZDUSD, USDCAD, USDJPY and XAUUSD.

**The 26-instrument target does not reconcile yet.** The target is 25 FX pairs plus
XAUUSD, and no list of them has ever been written down. Nine further pairs are named
as unavailable (USDCHF, NZDJPY, NZDCAD, NZDCHF, EURNZD, GBPNZD, AUDNZD, CADJPY,
CADCHF), which makes 27, one more than the target. The 28 crosses of the eight
currencies also include EURCHF and GBPCHF, which are in neither list. Which 25 pairs
are meant is a product decision. `universe.coverage_summary()` reports this state and
a test fails the day the numbers change without it being answered.

NZD rests on one pair and CHF on two until then, and both stay flagged.

D1 bars are UTC-midnight bins, not the 17:00 New York convention described in
`research/instruments.py`.

## Verification

```
python tools/verify_market_xray.py --api http://127.0.0.1:8000 \
                                   --web http://127.0.0.1:5173
python tools/verify_market_desktop.py            # the native desktop app
python tools/verify_market_desktop.py --quit-after
python tools/verify_market_integration.py --api http://127.0.0.1:8000
python tools/verify_backend_ownership.py         # adoption, conflicts, cold start
python tools/verify_bundle_dependencies.py       # DLLs a clean machine would lack
python tools/verify_installer.py                 # the real NSIS installer
cd apps\backend && python -m pytest tests/test_market_xray.py -q
```

`verify_market_xray.py` drives the dashboard in Chromium — including reading
canvas pixels to confirm the X-Ray actually paints green and red curves — and
writes screenshots to `artifacts/market-xray/`.

`verify_market_desktop.py` targets the *native* application, not a browser. It
finds the Market window inside `tars-companion.exe`, captures it with
`PrintWindow`, drives fullscreen with a real F11 keystroke, resizes, closes and
reopens it, and checks that exactly one supervised backend runs as a child of
the shell. Evidence lands in `artifacts/market-desktop/`.

### Verification still pending

Three things are **not** verified. They are listed here because the rest of
this page describes checks that were actually run, and these were not.

| Pending | Why it is still open |
|---|---|
| **Manual tray-icon click** | Windows 11's tray overflow is an XAML island that resists UI Automation — a pre-existing gap in this repo, recorded in `docs/demo/CAMPUS_DEMO.md` ("Tray icon click and the tray menu are implemented but not automated; verify by eye"). The tray item's handler calls the same `open_market_impl` as the verified `--market` and startup paths, but no test drives the click. Needs a human to click it once and confirm the window opens. |
| **Clean-machine install** | `tools/verify_installer.py` runs the real installer with PATH cut to Windows and an empty data root, on this development machine. That is not a clean machine: system DLLs and the WebView2 runtime cannot be isolated, and Windows 11 Home has neither Sandbox nor Hyper-V. Needs a real clean Windows install. |
| **Real-feed LIVE and STALE** | Needs a logged-in MT5 account and an open market. |

Until each is actually performed, neither should be described as verified.

## Storage

| What | Checkout | Installed |
|---|---|---|
| Candles | `<data root>\market\xray_store\<SYMBOL>\<TF>.parquet` | the same |
| Zones, events, strength history, feed health | `apps/backend/storage/market_xray.db` | `%LOCALAPPDATA%\com.tars.companion\storage\` |
| Backend log | console | `%LOCALAPPDATA%\com.tars.companion\logs\backend.log` |
| UI preferences | `localStorage` | `localStorage` |

The database used to land in `apps/backend/apps/backend/storage`, by accident of a
path relative to the working directory. Anything there is copied to the correct place
on first start and the original is left in place.

