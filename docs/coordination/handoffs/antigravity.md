# Handoff — Antigravity

Owned directory: `apps/web/`. Only Antigravity edits this file. See
[AGENTS.md](../../../AGENTS.md) for the full handoff protocol.

Update this file at the end of every session working on `apps/web/`, using
the template below. Keep only the latest handoff at the top; older entries
may be kept below a `---` separator for history, but are not required
reading for the next session (`CURRENT_STATE.md` is authoritative for that).

---

## Latest handoff (TARS CORE EXPERIENCE — FINAL INTEGRATION PASS)

**Status**: COMPLETE — Integrated backend presentation contract (`display_text` / `speech_text` / `quality`), voice telemetry correlation IDs, streaming display/speech separation, and full verification suite pass.
**Branch**: `feature/tars-core-experience-integration`
**Commit SHA**: `f2801da287e02e078519fc57593c66fe85303ec3`
**Integrated Backend Base**: `a57544acc520cb20139f38982a9b79ba4b79bfa8`
**Integrated Native/Web Base**: `8c576e6462719c8ca60b135bb5f9bc68e30b6567`

**Work completed**:
1. **Integration Branch Setup & Cherry-Pick**:
   - Created dedicated integration branch `feature/tars-core-experience-integration` branched from `feature/tars-core-experience-recovery`.
   - Cherry-picked backend commit `a57544acc520cb20139f38982a9b79ba4b79bfa8` cleanly with zero conflicts (`d9e2805`).
2. **Backend Presentation Contract Consumption (`display_text` & `speech_text`)**:
   - Updated `apps/web/src/types/assistant-message.ts` to include `display_text`, `speech_text`, and `AssistantResponseQuality` types.
   - Upgraded `AssistantClient` (`apps/web/src/runtime/AssistantClient.ts`) to query `/api/v2/assistant/query` by default (falling back to `/api/v1/assistant/query` if 404), extracting `display_text`, `speech_text`, and `quality`.
   - Updated streaming SSE consumption on `/api/v1/assistant/query/stream` to receive delta chunks for streaming UI updates and complete payloads carrying `display_text` and `speech_text`.
   - Separated presentation layers: `display_text` is rendered through `MarkdownContent` in Chat and Workstation views; `speech_text` is fed directly to TTS upon turn completion with frontend `composeSpeech()` retained strictly as a fallback.
3. **Voice & Wake Telemetry ID Correlation**:
   - Updated Rust native wake engine (`apps/web/src-tauri/src/wake_engine.rs`) to extract `telemetry_id` from backend `/api/v1/voice/transcribe` JSON responses and emit it in `WakeTimingTelemetry` payloads (`tars://wake-state-changed`).
   - Added `telemetry_id` to TypeScript `WakeTimingTelemetry` interface.
   - Propagated telemetry turn IDs as `X-TARS-Voice-Turn-ID` HTTP headers on all assistant queries to correlate voice transcription turns with assistant LLM routing and voice telemetry traces.
4. **Canonical Single-Utterance Wake Preservation**:
   - Preserved Antigravity's single-utterance wake dispatch model: requests like "Hey TARS, analyze the chart" emit exactly one event (`tars://analyze-chart-detected` or `tars://command-transcript`) with no duplicate dispatches.
5. **Full Multi-System Verification**:
   - Backend Pytest: 643 passed, 0 failed.
   - Backend Ruff: All checks passed.
   - Backend MyPy: 105 source files checked with 0 errors.
   - Frontend Vitest: 23 test files, 147 passed, 0 failed.
   - Frontend TypeScript Typecheck: `tsc --noEmit` passed with 0 errors.
   - Frontend Production Build: `vite build` generated full PWA distribution without errors.
   - Native Rust Tests: 6 passed, 0 failed.
   - Native Release Compilation: `cargo build --release` built `tars-companion.exe` (12.8 MB) cleanly.
   - Contract & Blocker Checks: `python tools/core_experience_checks.py` passed with 0 findings.
   - One-Command Launcher: `scripts/start_tars.ps1` verified clean port check, .env loading, provider validation, backend health, and native window launch.

**Work completed**:
1. **Priority 1: Native Wake State Machine & Single-Utterance Extraction (`apps/web/src-tauri/src/wake_engine.rs`)**:
   - Replaced basic enum with explicit 7-state `WakeState`: `Idle`, `Audio`, `Transcribing`, `WakeDetected`, `CommandListening`, `Processing`, `Speaking`.
   - Added timing and latency instrumentation: `audio_detected_at`, `speech_end_at`, `transcription_start`, `transcription_complete`, `wake_detected_at`, `command_ready_at`.
   - Implemented single-utterance trailing command extraction: trailing phrase after "Hey TARS" is extracted and dispatched via ONE canonical event (`tars://analyze-chart-detected` or `tars://command-transcript`), eliminating duplicate event triggers.
   - Handled two-stage flow: if trailing text is empty, transitions to `CommandListening` and emits `tars://wake-detected`.
2. **Priority 2: Startup Launcher Hardening (`scripts/start_tars.ps1`)**:
   - Added worktree `.env` auto-discovery (from parent workspace or `.env.example` fallback) preventing worktree launch crashes.
   - Enforced build provenance verification for `tars-companion.exe` and verified `MainWindowHandle` visibility before claiming launch success.
   - Gated `[7/7] TARS READY` strictly behind verified `$readiness.ready -eq $true`.
3. **Priority 4: Speech Sanitization & Markdown Cleaning (`apps/web/src/services/speech.ts`)**:
   - Implemented `composeSpeech(displayText, limit)` TypeScript helper to clean Markdown markers, headers, bullet asterisks, URLs, Windows/Unix file paths, and omit raw code blocks.
   - Refactored `VoiceAssistantRuntime.tsx` streaming loop to accumulate chunks until complete speakable sentences are detected (`/[^.!?]*[.!?]+\s*/g`) before sanitizing and synthesizing to TTS.
   - Sanitized manual read-aloud handler in `AssistantMessage.tsx`.
   - Updated `MarkdownContent.tsx` to handle partial streaming markdown delimiters cleanly.
4. **Priority 5: Kokoro Voice Candidate UI (`apps/web/src/components/voice/VoiceControlView.tsx` & `SettingsView.tsx`)**:
   - Truthfully exposed candidate audition cards for `am_michael` (A), `am_onyx` (B), `bm_george` (C), and `af_heart` (Reference) with interactive Listen buttons.
   - Prevented fake dynamic live swapping and surfaced real runtime readiness provider cards.
5. **Priority 6: Real Runtime States & Native Bridge (`apps/web/src-tauri/src/lib.rs`, `WakeClient.ts`)**:
   - Added `set_wake_playback_state` command to sync frontend audio playback state directly into Rust native state machine (`Processing` -> `Speaking` -> `Idle`).
   - Changed default `compactMode` to `false` in `storage.ts` so initial layout preserves full workstation interface dimensions.

**Files changed**:
- `apps/web/src-tauri/src/lib.rs`
- `apps/web/src-tauri/src/wake_engine.rs`
- `apps/web/src/components/assistant/AssistantMessage.tsx`
- `apps/web/src/components/assistant/MarkdownContent.tsx`
- `apps/web/src/components/settings/SettingsView.tsx`
- `apps/web/src/components/voice/VoiceControlView.tsx`
- `apps/web/src/runtime/VoiceAssistantRuntime.tsx`
- `apps/web/src/runtime/WakeClient.ts`
- `apps/web/src/services/speech.ts` (NEW)
- `apps/web/src/services/storage.ts`
- `apps/web/src/test/single-utterance-wake.test.ts` (NEW)
- `apps/web/src/test/speech.test.ts` (NEW)
- `scripts/start_tars.ps1`
- `docs/coordination/handoffs/antigravity.md`

**Interfaces exposed**:
- `WakeState` (7-state enum), `WakeTimingTelemetry`, `set_wake_playback_state(speaking: bool)`, `composeSpeech(displayText, limit)`, `onWakeStateChanged` listener.

**Tests run**:
- **Vitest Unit & Integration**: `npm --prefix apps/web test -- --run` -> **147 passed across 23 test files (100%)**.
- **TypeScript Typecheck**: `npm --prefix apps/web run typecheck` -> **0 errors**.
- **Production Bundle Build**: `npm --prefix apps/web run build` -> **Built in 7.81s (PWA precache generated)**.
- **Cargo Tests**: `cargo test --manifest-path apps/web/src-tauri/Cargo.toml` -> **6 passed, 0 failed, 1 ignored (WGC live capture)**.
- **Cargo Release Build**: `cargo build --release --manifest-path apps/web/src-tauri/Cargo.toml` -> **Finished release profile [optimized] target(s) in 7m 35s, binary `tars-companion.exe` created (12.8MB)**.
- **Blocker Inspections**: `python tools/core_experience_checks.py` -> **All 11 frontend/launcher/wake/speech findings resolved (remaining finding is Codex backend response_quality contract)**.

**Known limitations**:
- Background wake word listening on iOS PWA remains bounded by mobile OS sandbox constraints (ADR-007).
- Backend dynamic voice changing is exposed as user preference candidate audition until backend multi-voice synthesis endpoint is fully connected by Codex.

**Exact dependencies required from other agents**:
- `claude.md` / `codex.md`: Wire `apps/backend/assistant/response_quality.py` (`ResponseQualityContract`) on the backend side in parallel.

**Next recommended action**:
- Hand off to coordinator for integration verification. Do not merge automatically into `integration/v1` or `main`.

---

## Older handoffs
