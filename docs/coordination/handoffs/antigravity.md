# Handoff — Antigravity

Owned directory: `apps/web/`. Only Antigravity edits this file. See
[AGENTS.md](../../../AGENTS.md) for the full handoff protocol.

Update this file at the end of every session working on `apps/web/`, using
the template below. Keep only the latest handoff at the top; older entries
may be kept below a `---` separator for history, but are not required
reading for the next session (`CURRENT_STATE.md` is authoritative for that).

---

## Latest handoff (PARTIAL)

**Status**: PARTIAL — Frontend, Tauri 2, and PWA foundation complete; live backend integration and final browser/E2E verification pending.
**Branch**: `feature/v1-web-pwa`
**Base SHA**: `ad6f4bf6bd4dcb5c4039450dc8b8540ce63108e7`
**Work completed**:
- Scaffolded single unified React 18 + TypeScript + Vite codebase in `apps/web/` powering Windows Tauri 2 desktop shell and iPhone installable/responsive PWA (ADR-010).
- Configured Tauri 2 desktop integration (`src-tauri/tauri.conf.json`, `Cargo.toml`, `src/main.rs`, `src/lib.rs`, `capabilities/default.json`) with native window minimization, compact companion resizing, and tray hooks.
- Configured PWA manifest, service worker generation via `vite-plugin-pwa`, iOS safe-area handling (`env(safe-area-inset-top)` / `env(safe-area-inset-bottom)`), and responsive touch targets.
- Created canonical TypeScript types and strict runtime contract validators for `contracts/trading-event.schema.json` and `contracts/assistant-message.schema.json`, rejecting `schema_version !== "1.0.0"`, forbidden extra properties, and invalid states.
- Implemented typed `TARSWebSocketClient` supporting automatic reconnection with exponential backoff and jitter, ping/pong latency measurement, protocol error reporting, and dynamic endpoint configuration (localhost, LAN, Tailscale Serve URL per ADR-014).
- Built interactive TARS deterministic visual SVG character with responsive mood states: `IDLE`, `LISTENING`, `THINKING`, `SPEAKING`, `ALERT`, `WARNING`.
- Built full trading companion views:
  1. **Companion/Home**: Hero spotlight with TARS character, active setup spotlight, quick voice/text console, advisory banner, and personality dials.
  2. **Active Setups**: Parameter cards with symbol, direction, entry, stop loss, take profit, R:R, risk %, reason codes, warnings, and filters (zero fake AI confidence scores per ADR-004).
  3. **Alert History**: Real-time event feed with search and canonical JSON payload inspector drawer.
  4. **Ask TARS**: Conversational turn-by-turn chat with provider attribution badges (`stt`, `assistant`, `tts`) and speech read-out.
  5. **Voice Controls**: Push-to-Talk orb with live audio frequency/volume meters and iOS background listening limitation disclaimer (ADR-007).
  6. **Memory & Research**: SQLite FTS5 / Obsidian notes query shell with explicit boundary markers (ADR-013).
  7. **System Status**: Configurable WebSocket endpoints, connection health, round-trip latency, platform detector, and protocol error logs.
  8. **Settings**: Audio parameters, mock event fixture generator controls, and compact HUD toggles.
  9. **Compact HUD**: Floating always-on companion widget for chart side-by-side trading.
- Implemented development fixture/mock generator emitting schema-valid events for zero-backend standalone testing.
- Created unit and component test suites (`contracts.test.ts`, `websocket.test.ts`, `components.test.tsx`) — 12/12 tests passing.
- Verified TypeScript typechecking (`tsc --noEmit`) and production bundling (`vite build`) producing valid static and PWA output.

**Files changed**:
- `apps/web/package.json`
- `apps/web/package-lock.json`
- `apps/web/tsconfig.json`
- `apps/web/vite.config.ts`
- `apps/web/vitest.config.ts`
- `apps/web/index.html`
- `apps/web/public/`
- `apps/web/src-tauri/`
- `apps/web/src/types/`
- `apps/web/src/contracts/`
- `apps/web/src/services/`
- `apps/web/src/components/`
- `apps/web/src/styles/`
- `apps/web/src/test/`
- `apps/web/src/App.tsx`
- `apps/web/src/main.tsx`
- `docs/coordination/handoffs/antigravity.md`

**Interfaces exposed**:
- `validateTradingEvent(raw: unknown)`: Validates raw payloads against canonical trading event schema.
- `validateAssistantMessage(raw: unknown)`: Validates raw payloads against canonical assistant message schema.
- `TARSWebSocketClient`: Typed event client managing connection lifecycle and event subscription.
- `audioService`: Push-to-talk microphone capture, audio visualizer stream, and speech synthesizer.
- `sendNotification(options)`: Unified Tauri native and web notification dispatcher.
- `createMockTradingEvent(custom)`: Fixture generator for schema-valid events.

**Tests run**:
- `vitest run` — 12 unit/component tests passing (contract validation, schema violation catching, WebSocket client lifecycle, TARS character states, setups filtering, compact HUD).
- `tsc --noEmit` — 0 TypeScript compilation errors.
- `npm run build` — Production Vite build succeeded (manifest, registerSW, chunks).

**Known limitations**:
- Marked PARTIAL: Live integration with `apps/backend/` FastAPI WebSocket endpoint and full end-to-end multi-agent validation will occur in Wave 2.
- Local Windows runner does not have `cargo` installed, so Tauri native binary compilation was checked via configuration and typecheck rather than `cargo tauri build`.
- Browser subagent was stopped per user request; preliminary screenshots confirmed layout and interaction states.

**Exact dependencies required from other agents**:
- Claude Code (`apps/backend/`): Live WebSocket endpoint broadcasting events matching `contracts/trading-event.schema.json` and `contracts/assistant-message.schema.json`.
- Codex (`tests/`, `tools/`): Contract verification harness across WebSocket boundary.

**Next recommended action**:
- Coordinate with Claude Code to connect live WebSocket stream to `ws://127.0.0.1:8000/ws/events`.
- Perform cross-agent integration test run once backend is active.

---
