# Handoff — Antigravity

Owned directory: `apps/web/`. Only Antigravity edits this file. See
[AGENTS.md](../../../AGENTS.md) for the full handoff protocol.

Update this file at the end of every session working on `apps/web/`, using
the template below. Keep only the latest handoff at the top; older entries
may be kept below a `---` separator for history, but are not required
reading for the next session (`CURRENT_STATE.md` is authoritative for that).

---

## Latest handoff (TARS Market Intelligence: Desktop Hardening & Production Lifecycle)

**Status**: COMPLETE — Hardened Tauri 2 desktop shell, verified backend adoption and ownership state machine, safe shutdown protocol with supervisor secret, single loopback port endpoint negotiation, cold-start `--market` window focus, feed provenance badges, native desktop verification (17/19 checks passed, 0 failed, 2 unverified due to headless foreground lock).
**Branch**: `feature/desktop-supervisor-hardening`
**Commit SHA**: `076842a99f167d5d4b944d20e137a8b558e8edb3` (implementation commit `b6241d0106071eb613000edd87bbb333f5863966`)
**Starting Base**: `110d07e` (`origin/feature/market-xray-phase12-integration`)

**Work completed**:
1. **P1-A1 Safe Backend Adoption & Compatibility Validation (`apps/web/src-tauri/src/supervisor.rs`)**:
   - Implemented strict identity schema verification querying `GET /api/v1/runtime/identity`.
   - Verified `service == "tars-backend"` and `contract_version == 1` before treating any running backend as compatible. An HTTP 200 from a foreign or unversioned server is classified as `Foreign` (`BackendDecision::Conflict`) and never touched.
   - Distinctly separated process compatibility from process ownership: an existing compatible backend is adopted as `Attached` (`ownership == ATTACHED`), whereas only a child spawned by this shell is marked `Owned` (`ownership == OWNED`).
   - Stored ownership metadata in `backend-owner.json` under local app data.

2. **P1-A2 Safe Shutdown & Authorization Header (`apps/web/src-tauri/src/supervisor.rs`, `apps/backend/app/identity.py`, `routers/lifecycle.py`)**:
   - `BackendSupervisor::stop()` enforces safe termination: it checks `ownership == OWNED` before issuing shutdown commands or terminating processes.
   - If `ownership == ATTACHED`, the backend process is left completely untouched on shell exit.
   - Shutdown requests to `/api/v1/runtime/shutdown` transmit the random secret generated at spawn time via the `X-TARS-Supervisor-Secret` header, validated by backend `shutdown_authorized()`.
   - Implemented `reap_exited_child()` to cleanly join and reap terminated child process handles without leaving zombie processes.

3. **P1-A3 Port Consistency & Endpoint Negotiation (`apps/web/src-tauri/src/backend_endpoint.rs`, `tauri.conf.json`)**:
   - Standardized backend endpoint resolution in native Rust via `backend_endpoint.rs`, accepting `TARS_BACKEND_URL` and `BACKEND_PORT` with loopback validation.
   - Injected runtime script setting `window.__TARS_BACKEND_URL__` on every webview initialization.
   - Updated `tauri.conf.json` CSP to support loopback wildcard ports (`http://127.0.0.1:*`, `ws://127.0.0.1:*`), enabling custom developer or test ports without security policy rejections.
   - Updated Vitest CSP regex in `src/test/tauri.test.ts`.

4. **P1-A5 Cold-Start `--market` Support (`apps/web/src-tauri/src/lib.rs`, `scripts/start-market-desktop.ps1`)**:
   - Fixed defect where launching `tars-companion.exe --market` on a fresh instance only opened the companion orb.
   - Added cold-start command-line inspection (`std::env::args().any(|a| a == "--market")`) in `lib.rs` `setup()`, guaranteeing Market Intelligence opens immediately on fresh starts.
   - Handled single-instance IPC callback to unminimize and focus the existing Market window when already running.
   - Updated `scripts/start-market-desktop.ps1` to pass `-ArgumentList '--market'`.

5. **P2-A1 Feed-Status & Provenance UI (`apps/web/src/marketxray/`)**:
   - Integrated backend data provenance contract in `types.ts`, `api.ts`, and `MarketXRayApp.tsx`.
   - Added distinct provenance badges in `TopNav.tsx`: `LIVE FEED` (green pulse) vs `RECORDED STORE (HISTORICAL)` (cyan outline).
   - Surfaced `REPLAY PINNED` badge when historical cursor is pinned.
   - Handled empty candle stores gracefully with actionable guidance banner pointing to `python -m market.ingest`.
   - Guaranteed deterministic UI: recorded history is never mislabeled as live; trading execution remains permanently labeled `DISABLED`.

6. **P1-A4 Windows Job Objects Evaluation**:
   - Evaluated Windows Job Objects (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) for desktop process tree management.
   - **Finding**: Assigning adopted (`ATTACHED`) backends to a Job Object with kill-on-close causes the Windows kernel to terminate third-party or developer-started backends whenever the Tauri shell closes, directly violating P1-A2. Furthermore, processes launched under modern IDEs/terminals without `JOB_OBJECT_LIMIT_BREAKAWAY_OK` trigger `ERROR_ACCESS_DENIED` on `AssignProcessToJobObject`.
   - **Recommendation**: Job Objects should strictly be restricted to *spawned* child processes (`ownership == OWNED`). The existing cooperative HTTP shutdown with authorization header (`X-TARS-Supervisor-Secret`) is the superior mechanism for clean database WAL flush and port release.

7. **Desktop Stability & Native Verification**:
   - Window sizing: Enforced `fit_market_window` with minimum 1024x680, preventing off-screen overflow on scaled displays.
   - Minimized window restoration: Implemented `unminimize()` and Win32 `AllowSetForegroundWindow(0xFFFFFFFF)` to prevent offscreen (-32000, -32000) startup bugs.
   - F11 Fullscreen: Added capturing-phase event listeners on `window` and `document` in `MarketXRayApp.tsx` mapped to `market_toggle_fullscreen`.
   - Executed `tools/verify_market_desktop.py` against production release bundle: **17 passed, 0 failed, 2 unverified** (F11 keystroke delivery unverified in headless console session due to Windows foreground security lock).

**Files changed**:
- `apps/web/src-tauri/Cargo.toml`
- `apps/web/src-tauri/tauri.conf.json`
- `apps/web/src-tauri/src/backend_endpoint.rs` (NEW)
- `apps/web/src-tauri/src/lib.rs`
- `apps/web/src-tauri/src/supervisor.rs`
- `apps/web/src/marketxray/MarketXRayApp.tsx`
- `apps/web/src/marketxray/api.ts`
- `apps/web/src/marketxray/desktop.ts`
- `apps/web/src/marketxray/styles.css`
- `apps/web/src/marketxray/types.ts`
- `apps/web/src/marketxray/components/TopNav.tsx`
- `apps/web/src/test/tauri.test.ts`
- `scripts/start-market-desktop.ps1`
- `docs/coordination/handoffs/antigravity.md`

**Interfaces exposed**:
- `BackendDecision` (`Spawn`, `Own`, `Attach`, `Replace`, `Conflict`, `Wait`)
- `BackendIdentity` (`service`, `contract_version`, `instance_id`, `build_id`, `app_version`, `owner_id`, `pid`)
- `Provenance` (`source`, `provider`, `is_live`, `as_of`, `pinned_replay`, `summary`)
- `market_toggle_fullscreen` Tauri invoke command
- `window.__TARS_BACKEND_URL__` global frontend injection

**Tests run**:
- **Native Desktop Verification**: `python tools/verify_market_desktop.py --quit-after` -> **17 passed, 0 failed, 2 unverified** (captured real pixels, verified single-instance, supervised backend child, port 8000 listener, store coverage, REPLAY provenance, resize to 1280x820, window coexistence, and clean cooperative shutdown leaving 0 orphan processes).
- **Frontend Vitest**: `npm --prefix apps/web test -- --run` -> **30 test files passed, 212 passed, 0 failed (100%)**.
- **Frontend Typecheck**: `npm --prefix apps/web run typecheck` (`tsc --noEmit`) -> **0 errors**.
- **Frontend Bundle**: `npm --prefix apps/web run build` (`vite build`) -> **Production bundle built in 9.12s**.
- **Rust Native Unit Tests**: `cargo test --manifest-path apps/web/src-tauri/Cargo.toml` -> **33 passed, 0 failed, 1 ignored**.
- **Backend Identity & Lifecycle Tests**: `pytest apps/backend/tests/test_identity.py apps/backend/tests/test_lifecycle_router.py` -> **32 passed, 0 failed**.
- **Production Executable Build**: `npm --prefix apps/web run tauri -- build --no-bundle` -> compiled release executable `tars-companion.exe` with embedded assets.

**Known limitations**:
- Windows UIPI foreground lock prevents background non-interactive scripts from synthesizing keystrokes to foreground windows without active user desktop interaction.

**Exact dependencies required from other agents**:
- `codex.md`: Completed contract verification of provenance schema and market lifecycle.

**Next recommended action**:
- Hand off to coordinator for integration sign-off.

---

## Older handoffs

