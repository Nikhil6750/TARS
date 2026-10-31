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

This needs the MSVC C++ build tools, because Tauri compiles a native binary.
The script checks for them first and tells you what to install if they are
missing. Once running, the window is also reachable any time from the tray
menu (**Market Intelligence**).

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

Source CSVs are read from `C:\Trade Data`; Parquet output goes to
`D:\TARS_DATA\market\xray_store`. The ingester only ever writes under that
store root — quant_brain's frozen research partitions
(`D:\TARS_DATA\market\local_xauusd`, `...\dukascopy`) are never modified.

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
cd apps\backend && python -m pytest tests/test_market_xray.py -q
```

The first drives the real dashboard in Chromium — including reading canvas
pixels to confirm the X-Ray actually paints green and red curves — and writes
screenshots to `artifacts/market-xray/`.

## Storage

| What | Where |
|---|---|
| Candles | `D:\TARS_DATA\market\xray_store\<SYMBOL>\<TF>.parquet` |
| Zones, events, strength history, feed health | `apps/backend/storage/market_xray.db` |
| UI preferences | `localStorage`, per machine |
