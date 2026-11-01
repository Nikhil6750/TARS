# October 27 desktop demo

Application source: `release/market-xray-final-rc`, `4211f70de1f8027348fcc0f07f8d2ac02ccd08b7`.
Includes Claude's `5f63cec69171127aa9c46c3bb7661625b396ff69` and the authorized saved-instrument correction.

The delivered folder contains the Windows executable, its bundled backend/Python
runtime, `data` (the single-instrument Dukascopy replay), `data-universe` (the
existing 18-instrument historical store), and `start_offline_demo.ps1`.
Keep these together. You can move the whole folder; no checkout, Python, Node,
Rust, broker login, or original `D:\TARS_REPLAY_20261027` path is required.
Windows with the WebView2 runtime is required. A genuinely clean Windows install
has not been tested.

From PowerShell opened in that folder:

```powershell
powershell -File .\start_offline_demo.ps1
```

Allow the bundled backend to start. The dashboard retries during startup. Click
**XAUUSD · 27 Oct 2026 · 15:30–19:30 IST** to open 10:00 UTC. Use Next Candle,
Step Back, Play/Pause, or the UTC seek field. H1 Play stops at 14:00 UTC. Each
step waits for real analysis, so playback can be slower than the timer interval.
The analysis is reconstructed now from historical Dukascopy quotes. It is not
a prediction recorded in October, OANDA data, live broker data, or evidence of
profitability. Candles appear only after their own timeframe closes.

Only XAUUSD is present in this replay store. Currency strength is unavailable,
and the Coverage panel explicitly says cross-instrument rankings are unavailable.
Any XAUUSD leader card is relative only to this one-instrument store.

**Known release blocker:** the persistent journal can retain events from a
previously used store and attribute them to the new store's provider. Do not
treat journal rows or stored-event counts as evidence for this dataset. The
replay candles and X-Ray were independently checked; the journal isolation fix
is still required before an unconditional release.

To demonstrate all 18 labels, recorded currency strength and recorded rankings:

```powershell
powershell -File .\start_offline_demo.ps1 -Quit
powershell -File .\start_offline_demo.ps1 -DataRoot .\data-universe
```

This second store contains 17 FX pairs plus gold and ends in September 2026.
Its October replay shortcut is correctly disabled. Thin coverage warnings remain.
Quit before changing data roots; a second invocation of a running app opens the
existing window and does not replace its backend configuration.

To stop either demo:

```powershell
powershell -File .\start_offline_demo.ps1 -Quit
```

The NSIS installer ships the same application and backend runtime. It deliberately
does not import or copy this market history into a user's normal data store. To
use the supplied replay with an installed copy, quit the portable copy, then use
the launcher with `-AppDirectory` pointing to the installed folder containing
`tars-companion.exe`, and `-DataRoot` pointing to this demo folder's `data`.
No developer-machine source path is needed at runtime.

Trading execution is permanently **DISABLED**. MT5 authentication, real-feed
LIVE/STALE, a manual tray click, and a clean Windows installation remain unverified.
