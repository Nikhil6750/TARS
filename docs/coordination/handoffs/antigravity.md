# Handoff — Antigravity

Owned directory: `apps/web/`. Only Antigravity edits this file. See
[AGENTS.md](../../../AGENTS.md) for the full handoff protocol.

Update this file at the end of every session working on `apps/web/`, using
the template below. Keep only the latest handoff at the top; older entries
may be kept below a `---` separator for history, but are not required
reading for the next session (`CURRENT_STATE.md` is authoritative for that).

---

## Latest handoff (CERTIFIED REMEDIATION COMPLETE)

**Status**: COMPLETE — Frontend & Tauri V1 certification blockers resolved; real WebSocket adapter implemented; certified microphone -> STT -> Assistant -> TTS audio pipeline verified; fabricated metrics eliminated; Tauri 2 configs validated; regression test suites passing (32/32).
**Branch**: `fix/v1-frontend-cert-blockers`
**Base SHA**: `e8308d82361e6df3c0928c4994ac0505a2e4235f`
**Final SHA**: `e0f9841` (prior to handoff commit)

**Work completed**:
1. **WebSocket Protocol Adapter**:
   - Reconciled WebSocket message parsing with real backend `EventBus.broadcast` envelope: `{ type: 'trading_event', event: { ... }, active_state_change: '...' }` as well as direct event schemas.
   - Handled backend `active_snapshot` envelope on connect/reconnect with `onActiveSnapshot` listener and hydrated active setups collection.
   - Supported ping/pong latency measurement and explicit `reconnect()` method with exponential backoff and jitter.
   - Proved zero fallback to mock mode unless `settings.mockGeneratorActive` is explicitly enabled.

2. **Real Push-to-Talk & Audio Pipeline**:
   - Completely removed hard-coded phrases (e.g. "Show active setups").
   - Implemented real microphone capture producing 16-bit PCM WAV Blobs (`encodeWAV()`).
   - Implemented certified voice pipeline:
     Microphone audio Blob -> POST `/api/v1/voice/transcribe` (multipart/form-data) -> receive transcript -> POST `/api/v1/assistant/query` -> receive assistant response -> POST `/api/v1/voice/synthesize` -> receive binary WAV audio bytes -> Web Audio playback.
   - Proved in regression test `audio-flow.test.ts` that the exact recorded microphone Blob is sent as the payload to backend STT, and that browser `speechSynthesis` is NOT used on the certified path when backend TTS responds.

3. **Elimination of Fabricated Metrics**:
   - Removed invented Sharpe (2.12), DSR (>1.8), and performance claims from `MemoryView.tsx`.
   - Enforced grounded display in real mode: `MemoryView` only displays records returned by backend SQLite/Obsidian search or a clean empty state.
   - Demo fixtures appear only when `mockModeActive: true` is explicitly configured, badged with `[DEMO/MOCK]` title and `#DEMO` tag.
   - Verified that `ActiveSetupsView` and `CompanionHero` render only deterministic parameters (entry, stop loss, take profit, R:R, risk %) with zero fabricated confidence percentages.

4. **Tauri 2 Configuration & Platform Bridge**:
   - Updated `src-tauri/tauri.conf.json` with explicit `"label": "main"` matching `capabilities/default.json` (`"windows": ["main"]`).
   - Generated valid bundled icon assets in `src-tauri/icons/` (`icon.png`, `32x32.png`, `128x128.png`, `128x128@2x.png`, `icon.ico`, `icon.icns`).
   - Verified desktop platform bridge methods (`toggleCompactWindow`, `minimizeWindow`, `toggleMaximizeWindow`, `closeWindow`, `requestNotificationPermission`, `sendNotification`).
   - Execution Status:
     - IMPLEMENTED: Native window controls, compact mode (420x720 always-on-top), tray hooks, and native notification dispatcher.
     - EXECUTED: Full frontend Vite production build and Vitest suite under Node.js / JSDOM.
     - VERIFIED: Configuration schema, capability mappings, platform detection, and browser fallbacks. Native Windows `.exe` cargo compilation cannot execute on this host because `cargo` / Rust toolchain is not installed on the system PATH.

5. **Frontend Quality & Linting**:
   - Configured ESLint (`eslint.config.js`) using `@eslint/js` and `typescript-eslint`.
   - Added `"lint": "eslint src"` target to `apps/web/package.json`.
   - Verified 0 lint errors, 0 lint warnings.
   - Verified 0 TypeScript compilation errors (`tsc --noEmit`).
   - Verified clean production Vite PWA build (`npm run build`).

6. **Regression Tests**:
   - Added `src/test/audio-flow.test.ts` (microphone WAV capture, STT proof, assistant invocation, TTS playback).
   - Added `src/test/lifecycle.test.ts` (active setups developing -> valid transitions, invalidation removal on `SETUP_INVALIDATED` / `INVALID` / `EXPIRED`).
   - Added `src/test/metrics-and-memory.test.tsx` (real-mode absence of fake metrics, demo fixture labeling).
   - Added `src/test/tauri.test.ts` (tauri.conf.json & capabilities alignment, platform bridge).
   - Updated `src/test/websocket.test.ts` (real backend `trading_event`, `active_snapshot`, ping/pong, reconnect).
   - Total: 7 test files, 32 unit/component tests passing.

**Files changed**:
- `apps/web/package.json`
- `apps/web/package-lock.json`
- `apps/web/eslint.config.js`
- `apps/web/vitest.config.ts`
- `apps/web/src-tauri/tauri.conf.json`
- `apps/web/src-tauri/icons/`
- `apps/web/src/services/websocket.ts`
- `apps/web/src/services/audio.ts`
- `apps/web/src/components/memory/MemoryView.tsx`
- `apps/web/src/components/voice/VoiceControlView.tsx`
- `apps/web/src/components/assistant/AskTARSView.tsx`
- `apps/web/src/App.tsx`
- `apps/web/src/test/websocket.test.ts`
- `apps/web/src/test/audio-flow.test.ts`
- `apps/web/src/test/lifecycle.test.ts`
- `apps/web/src/test/metrics-and-memory.test.tsx`
- `apps/web/src/test/tauri.test.ts`
- `docs/coordination/handoffs/antigravity.md`

**Interfaces exposed**:
- `TARSWebSocketClient`: Unified client handling real backend trading event, active snapshot, ping/pong, and reconnect subscriptions.
- `audioService.transcribeAudio(audioBlob, apiEndpoint)`: Sends actual microphone WAV Blob to backend STT endpoint.
- `audioService.synthesizeAndPlay(text, apiEndpoint)`: Requests backend neural TTS audio and plays returned binary WAV bytes.
- `encodeWAV(samples, sampleRate)`: Encodes float samples into valid 16-bit PCM WAV container.
- `validateTradingEvent(raw)` / `validateAssistantMessage(raw)`: Strict runtime contract validation.

**Tests run**:
- `vitest run` — 7 test files, 32 passed (100%).
- `tsc --noEmit` — 0 errors.
- `npm run lint` (`eslint src`) — 0 errors, 0 warnings.
- `npm run build` — Successful Vite production bundle with PWA service worker.

**Known limitations**:
- Local environment does not have Rust / `cargo` on PATH; native desktop compilation was validated through configuration and schema verification.

**Exact dependencies required from other agents**:
- Claude Code (`apps/backend/`): Run FastAPI backend with `/ws/events`, `/api/v1/voice/transcribe`, `/api/v1/voice/synthesize`, and `/api/v1/assistant/query`.
- Codex (`tests/`, `tools/`): Contract verification harness across WebSocket boundary.

**Next recommended action**:
- Hand off to coordinator for integration verification across backend and frontend.

---
