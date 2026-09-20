# TARS campus demo runbook

Start: `powershell -ExecutionPolicy Bypass -File scripts/start_tars.ps1` (one command; reuses the built
native binary when the source is unchanged, rebuilds otherwise, ~2 min warm / ~10 min cold).
Use **headphones** — the mic path has no acoustic echo cancellation (an echo guard exists, but it is a mitigation).

## Before you go on stage
1. `.env` must exist at repo root (copy `.env.example`; `TTS_PROVIDER=pocket`, `FASTER_WHISPER_MODEL=tiny.en`).
   The launcher overwrites a `.env` that is byte-identical to `.env.example` with another worktree's file — edit at least one value.
2. Claude Code CLI logged in (`claude` on PATH) — it is the assistant provider.
3. Optional MT5: open the MetaTrader 5 terminal and log in to a (demo) account. Until then the HUD shows `MT5` amber (DISCONNECTED). Read-only; TARS never places orders.
4. Optional TradingView: keep a TradingView chart window visible; `TV` turns green once the watcher sees it.
5. Live economic calendar needs internet (`CAL` green). Offline → use DEMO REPLAY.

## Script
1. HUD shows `LISTENING`, indicator dots are truthful (green = live, amber = not connected).
2. Say: "TARS, what is happening with EURUSD?" — live partial words appear, answer streams and is spoken.
3. While it speaks say: "Stop. What about gold?" — old speech stops, new answer follows.
4. Proactive: `curl -X POST http://127.0.0.1:8000/api/v1/demo/replay/start` (or live calendar event). A native
   notification appears, the HUD pops with a `[DEMO REPLAY]` alert and the quote moves; Claude's read replaces the summary.
5. Follow up by voice: "What does that mean for EURUSD?" — the turn carries the alert and market context.

## Checks
- `python tools/verify_tray_lifecycle.py` — close-to-tray + hotkey (Ctrl+Shift+Space) against the running app.
- `python tools/realtime_e2e_ws.py` — real backend timing with synthetic speech (stop the desktop app first).
- `python tools/realtime_voice_test.py` — the human microphone test (reference for PASS).

## Known limits
- Speech-end → first audio is ~5 s on this laptop, dominated by the Claude CLI start (~3 s) and the final decode.
- Partials come from a small streaming model (sherpa-onnx); the final transcript is faster-whisper.
- Tray icon click and the tray menu are implemented but not automated; verify by eye.
- News: no live headline provider (`NEWS` shows NOT CONFIGURED). TradingView: window monitoring only, no webhook.
