# Market X-Ray

Deterministic Forex and XAUUSD market intelligence inside TARS. Every number
it shows is computed from recorded bid/ask candles on this machine. It places
no trades, needs no API key, and never labels recorded history as live.

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
                                            #   -- only while TARS is already running
tars-companion.exe --quit                   # cooperative stop, backend included
scripts\start-market-desktop.cmd -Dev       # Vite dev server + debug binary
scripts\start-market-desktop.cmd -Rebuild   # force a fresh release build
```

The window is also in the tray menu as **Market Intelligence**. A second
launch never starts a second copy: the single-instance plugin routes it to the
running app.

`--market` needs TARS to be running already. It is handled by the
single-instance plugin, which only fires on a *second* launch, so passing it
to a cold start gives you the companion and no Market window. Verified by
doing exactly that. Use the launcher for a cold start (it sets
`TARS_OPEN_MARKET=1`, which the startup hook does read), and `--market`
afterwards.

An installer is produced alongside the binary at
`apps/web/src-tauri/target/release/bundle/nsis/TARS_1.0.0_x64-setup.exe`.

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

Only a real feed can produce `LIVE`, and stored candles never reach that code
path:

| Status | Meaning |
|---|---|
| `LIVE` | A real feed ticked within the freshness window, market open. |
| `STALE` | Feed connected but its last tick has aged out. |
| `REPLAY` | Driven by recorded history. Always says how far behind it is. |
| `CLOSED` | Outside trading hours. |
| `NO_DATA` | Market open, nothing connected. |

As shipped there is no authenticated MT5 session, so the dashboard runs in
`REPLAY` against history ending 2026-09-14 (XAUUSD 2026-09-11) and states the
data's age in the banner.

## Replay

The cursor lives on the backend, so every client sees the same moment. All
panels are computed with `ts <= cursor`, applied in SQL rather than trimmed
afterwards — so rewinding cannot reveal a bar that had not completed yet.
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
| `WS /api/v1/market/ws` | `market.frame` every 5s |

Separately, `GET /api/v1/health` carries an `identity` block: `instance_id`
(this process), `build_id` and `build_source` (this code — the commit SHA
where the repository is readable, else the release version), `app_version`
and `started_at`. The desktop shell adopts whatever answers that endpoint, so
these are what let it tell the backend it expects from an orphan left behind
by an older build. `TARS_BUILD_ID` overrides the build id, for packaging to
stamp what it produced.

## Coverage

18 instruments have recorded bid/ask data here: AUDCAD, AUDCHF, AUDJPY,
AUDUSD, CHFJPY, EURAUD, EURCAD, EURGBP, EURJPY, EURUSD, GBPAUD, GBPCAD,
GBPJPY, GBPUSD, NZDUSD, USDCAD, USDJPY and XAUUSD.

Not available (no source files on this machine): USDCHF, NZDJPY, NZDCAD,
NZDCHF, EURNZD, GBPNZD, AUDNZD, CADJPY, CADCHF.

D1 bars are UTC-midnight bins, not the 17:00 New York convention described in
`research/instruments.py`.

## Verification

```
python tools/verify_market_xray.py --api http://127.0.0.1:8000 \
                                   --web http://127.0.0.1:5173
python tools/verify_market_desktop.py            # the native desktop app
python tools/verify_market_desktop.py --quit-after
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

Two things are **not** verified. They are listed here because the rest of
this page describes checks that were actually run, and these were not.

| Pending | Why it is still open |
|---|---|
| **Manual tray-icon click** | Windows 11's tray overflow is an XAML island that resists UI Automation — a pre-existing gap in this repo, recorded in `docs/demo/CAMPUS_DEMO.md` ("Tray icon click and the tray menu are implemented but not automated; verify by eye"). The tray item's handler calls the same `open_market_impl` as the verified `--market` and startup paths, but no test drives the click. Needs a human to click it once and confirm the window opens. |
| **Clean-install verification** | Everything was verified against a development tree with its toolchain, `node_modules`, a built candle store and a warm data root. The NSIS installer was produced but never installed on a machine without those. Needs `TARS_1.0.0_x64-setup.exe` run on a clean Windows profile, then Market Intelligence opened from the installed app. |

Until each is actually performed, neither should be described as verified.

## Storage

| What | Where |
|---|---|
| Candles | `D:\TARS_DATA\market\xray_store\<SYMBOL>\<TF>.parquet` |
| Zones, events, strength history, feed health | `apps/backend/storage/market_xray.db` |
| UI preferences | `localStorage`, per machine |
