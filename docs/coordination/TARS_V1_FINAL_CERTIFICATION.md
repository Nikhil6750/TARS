# TARS V1 Final Certification

## Release decision

**Final verdict: NOT_CERTIFIED**

Target product SHA: `9f13e09a7c44be27901d34cb885a63ceb8755463`

Certification branch: `cert/v1-final-9f13e09`

Certification report commit: the commit containing this file

Certification date: 2026-08-16 (Asia/Calcutta)

The product was not promoted. `integration/v1` and `main` were not modified.

## Classification key

- **VERIFIED**: independently exercised or proven against the exact candidate.
- **IMPLEMENTED BUT UNVERIFIED**: implementation exists, but the required runtime path could not be exercised.
- **ENVIRONMENT GAP**: verification was prevented by unavailable host tooling, with no contrary product evidence.
- **BLOCKER**: product or release-gate defect that prevents certification and promotion.

## Git provenance

**VERIFIED**

- `origin/feature/v1-remediation-integration` resolved exactly to the target SHA after `git fetch --prune origin`.
- A fresh worktree was created at `C:\TARS-Final-Cert` on `cert/v1-final-9f13e09`, based exactly on the target SHA.
- The worktree was clean before certification.
- Required remediation ancestors were present:
  - Claude remediation: `c96bd2d05c64d89616d4c93719fa22298a105f6d`
  - Antigravity remediation: `0459fc52af2feb4af9f56bd29b27bbecfa92847c`
  - Codex hardening: `427fab4fc76c5c87267a0e7f046e0bb68c0b7538`
- Frozen contract blob IDs matched `origin/integration/v1` exactly:
  - `contracts/assistant-message.schema.json`: `7fd1c4946d40c8fc272bf2fb56deed294da35f04`
  - `contracts/trading-event.schema.json`: `e74d69ed46e776607ade14c8f41f288507da60ff`
- Candidate history contains the expected backend, frontend, quality/remediation, and coordination changes. No canonical contract change was present.

## Tool and runtime versions

- OS: Windows 10.0.26200 x86_64
- Python: 3.13.3
- pytest: 8.3.3
- Ruff: 0.16.2
- MyPy: 2.3.0
- datamodel-code-generator: 0.73.0
- json-schema-to-typescript: 15.0.4
- Node.js: 22.12.0
- npm: 10.9.0
- Vitest: 3.2.7
- TypeScript: 5.9.3
- ESLint: 9.39.5
- Vite: 6.4.3
- faster-whisper: 1.2.1
- kokoro-onnx: 0.5.0
- Pipecat: 1.7.0
- Tauri JavaScript API: 2.11.1
- Tauri CLI: 2.11.4

## Fresh quality gates

### Backend

**VERIFIED**

- `python -m pytest apps/backend/tests`: 75 passed, 0 failed, 0 skipped; one Starlette/httpx deprecation warning.
- `python -m ruff check apps/backend tests tools`: passed.
- `python -m mypy --config-file apps/backend/pyproject.toml apps/backend`: passed; 57 source files checked.

### Contracts

**BLOCKER**

- `python tools/generate_contracts.py --check`: failed.
- Reported drift:
  - `tools/generated/python/__init__.py`
  - `tools/generated/typescript/assistant-message.d.ts`
  - `tools/generated/typescript/trading-event.d.ts`
- The pinned generators were installed and used. A diagnostic regeneration proved content is semantically identical when end-of-line whitespace is ignored; the failure is CRLF checkout content versus LF temporary generation.
- Global Git configuration has `core.autocrlf=true`. The repository has no attributes/normalization that makes this mandatory Windows check reproducible.
- The required command exits 1 on the supported Windows certification host, so the contract/codegen gate does not pass.

### Frontend

**VERIFIED**

- `npm --prefix apps/web ci`: passed; 555 packages installed, 0 vulnerabilities.
- `npm --prefix apps/web run test`: 32 passed, 0 failed, 0 skipped across 7 files.
- `npm --prefix apps/web run typecheck`: passed.
- `npm --prefix apps/web run lint`: passed.
- `npm --prefix apps/web run build`: passed; production Vite/PWA output generated.

### Codex hardened suite

**BLOCKER**

- `python -m pytest tests/`: 52 passed, 1 failed, 24 skipped.
- The failure is `tests/contracts/test_codegen_drift.py`, caused by the Windows EOL reproducibility defect above.
- The 24 standalone skips are the external acceptance cases, which require harness-provided live processes. The external harness subsequently executed all 24 and all passed; no acceptance blocker remained skipped in the harness run.

### External certification

**BLOCKER**

- `python tools/run_certification.py`: exit 1.
- Passing sub-gates: codegen npm install, backend tests, Ruff, MyPy, frontend install/tests/typecheck/lint/build, Tauri static compatibility script, and external acceptance.
- Failing sub-gates: contract/codegen drift and the certification test group containing that drift test.
- External acceptance: 24 passed, 0 failed, 0 skipped.

## Lifecycle certification

**BLOCKER — FAIL**

The intended `SETUP_DEVELOPING -> SETUP_VALID -> SETUP_INVALIDATED` flow passed its happy-path acceptance case: three distinct event IDs were observed, the original valid event remained in history, invalidation was separate, and active state cleared.

However, history is not append-only. `apps/backend/events/service.py` persists with `INSERT OR REPLACE INTO trading_events`, while `event_id` is the table primary key. An independent probe through the actual public API submitted two schema-valid events with the same ID:

- first response: 201 (`SETUP_VALID`)
- second response: 201 (`SETUP_INVALIDATED`)
- resulting history rows for the symbol: 1
- original `SETUP_VALID` retained: false

Historical evidence can therefore still be overwritten by a duplicate event ID. This directly violates the release criterion and is a backend product blocker.

## WebSocket certification

**VERIFIED — PASS**

- Actual backend and actual shipped TypeScript client were exercised together.
- `active_snapshot`, `SETUP_DEVELOPING`, `SETUP_VALID`, and `SETUP_INVALIDATED` were consumed without protocol errors.
- Backend `ping`/`pong` produced a positive client latency measurement.
- Offline transition and reconnect were exercised.
- Two simultaneous clients received the same lifecycle events.
- Source inspection confirmed the backend envelope is `{type: "trading_event", event, active_state_change}` and the TypeScript client validates the enclosed canonical event before notifying consumers.

## Voice certification

### Voice plumbing

**VERIFIED — PASS**

The foreground path is implemented and exercised as:

`microphone/MediaRecorder Blob -> backend transcribe -> returned transcript -> backend assistant query -> assistant response -> backend synthesize -> WAV playback`

- The actual captured Blob is posted as multipart audio.
- The returned transcript is used as assistant input.
- No production hard-coded `"Show active setups"` substitution exists.
- Backend TTS bytes are played through an `Audio` element.
- Browser `speechSynthesis` exists only as an explicitly labeled error/offline fallback and was not invoked during the certified successful backend-TTS path.
- Deterministic frontend and external browser plumbing tests passed.

### Real STT

**VERIFIED — PASS**

`test_real_stt_transcribes_distinct_audio_to_distinct_non_hardcoded_text` passed using faster-whisper 1.2.1 and available local model assets.

### Real TTS

**VERIFIED — PASS**

`test_real_voice_round_trip_reaches_assistant_router_and_local_tts` passed using Kokoro 0.5.0 and available local model assets.

### Pipecat realtime transport

**IMPLEMENTED BUT UNVERIFIED**

Pipecat 1.7.0, Silero VAD, provider bridges, assistant routing, and a FastAPI WebSocket transport pipeline are present. The realtime `/api/v1/voice/session` audio transport was not exercised end-to-end with live audio in this certification. Package installation and unit-level construction are not treated as runtime proof.

## Security certification

**VERIFIED — PASS**

- The 1 MiB ASGI streaming ceiling passed for oversized declared `Content-Length`, missing `Content-Length`, and genuine chunked streaming bodies.
- Oversized streams are rejected with 413 during body receipt, before normal JSON/schema handling.
- Under-limit chunked invalid JSON reaches normal schema handling and returns 422.
- Malformed assistant payloads, unexpected contract fields, and secret/error reflection cases passed.
- No live trade/order execution endpoint was discovered; representative execution-like paths returned no execution surface.

## Memory provenance

**VERIFIED — PASS**

- Obsidian-compatible Markdown indexing into SQLite FTS5 is implemented.
- Vault-relative `source_id` survives retrieval and the actual assistant grounding chain.
- Retrieved context identifiers are retained without logging note contents.
- Missing-vault and absent-fact behavior passed.

## Trading truthfulness

**VERIFIED — PASS**

- Normal real mode contains no fabricated Sharpe, DSR, expectancy, win rate, drawdown, profitability, strategy-performance, or validation-result claims.
- The assistant refuses absent entry, stop, target, risk, performance, validation, and reason-code facts rather than inventing them.
- Frontend mock facts are disabled by default, isolated behind an explicit setting labeled `MOCK EVENT FIXTURE GENERATOR`, described as simulated, and canonical events visibly retain `source: mock`.

## Python 3.12 runtime

**ENVIRONMENT GAP — UNVERIFIED**

The project target remains Python 3.12. Only Python 3.13.3 (and obsolete Python 3.7) were installed on the certification host. No target change was made. All Python gates above ran under 3.13.3.

## Tauri / Cargo certification

### Tauri implementation

**BLOCKER — FAIL**

The React/Tauri shell, Rust entry points, build script, Tauri configuration, capabilities, notification/global-shortcut plugins, and npm package alignment are present. However, the native artifact set is not release-valid:

- `icon.ico` contains PNG bytes rather than ICO data.
- `icon.icns` contains PNG bytes rather than ICNS data.
- `128x128.png` and `128x128@2x.png` are both only 32x32 pixels.
- All six icon files are identical 109-byte PNG payloads.

### Tauri dependency resolution

**BLOCKER — FAIL**

`apps/web/src-tauri/Cargo.lock` is not a genuine complete Cargo resolution:

- only seven package records are present;
- only direct dependencies are listed;
- the transitive dependency graph required by Tauri is absent;
- crates.io `source` and `checksum` records are absent;
- the file claims `# This file is automatically @generated by Cargo`, but Git history shows it was introduced as the same 39-line incomplete artifact while Cargo was unavailable.

The static checker only verifies that a lockfile path exists; it does not validate lock completeness. This artifact must be regenerated by Cargo, never hand-written.

### Tauri native build

**ENVIRONMENT GAP — UNVERIFIED**

Rust, Cargo, rustup, Visual Studio Build Tools/MSVC, and the Windows SDK were not available. `tauri info` confirmed the missing toolchain. `cargo metadata`, `cargo check`, and a native Tauri build could not be run.

### Tauri runtime

**ENVIRONMENT GAP — UNVERIFIED**

WebView2 151.0.4129.86 is installed, but no native executable could be built or launched because the Rust/MSVC toolchain is absent.

## Warnings

- Node 22.12.0 is below `eslint-visitor-keys@5.0.1`'s declared minimum of Node 22.13.0 for the Node 22 line; npm emitted `EBADENGINE`, although frontend gates passed.
- npm reported a deprecated `glob@11.1.0` dependency warning.
- FastAPI tests emitted a Starlette/httpx deprecation warning.
- `pytest-asyncio` warned that the default fixture loop scope is unset in the root hardened run.
- The global Python environment has unrelated `ddgs`/`streamlit` dependency conflicts after installing the repository-pinned codegen dependencies; these did not cause the candidate failures.

## Environment gaps

- Python 3.12 interpreter unavailable.
- Rust/Cargo/rustup unavailable.
- Visual Studio Build Tools/MSVC and Windows SDK unavailable.
- Pipecat realtime audio transport not exercised end-to-end.

## Blocking defects and ownership

1. **Backend / Claude ownership** — Make trading-event history genuinely append-only. Reject duplicate event IDs (or otherwise preserve immutable facts); do not use `INSERT OR REPLACE` for audit history. Add a regression test that resubmitting an ID cannot replace the original event.
2. **Quality tooling / Codex ownership** — Make `generate_contracts.py --check` deterministic on Windows checkouts with `core.autocrlf=true` without weakening semantic drift detection. The exact mandatory command must pass from a clean supported worktree.
3. **Frontend/Tauri / Antigravity ownership** — Delete the misleading hand-authored resolution and regenerate a complete `Cargo.lock` with Cargo. Replace mislabeled/incorrect-size icon assets with valid native bundle assets, then run `cargo metadata`, `cargo check`, Tauri build, and native Windows launch on a properly provisioned host.

## Promotion status

- Certification report branch push: required after this report is committed.
- Product promotion to `integration/v1`: **not performed**.
- Post-merge sanity verification: **not applicable**.
- `main`: untouched.

## Final verdict

**NOT_CERTIFIED**

The remaining issues are not solely environmental. Append-only audit history can be overwritten, the mandatory contract drift gate fails on Windows, and the Tauri dependency/bundle artifacts are invalid or misleading. Promotion is prohibited until these product blockers are remediated and a new immutable candidate is independently certified.

## Next milestone after successful recertification

WAVE 2 — WINDOWS-WIDE TARS SYSTEM AGENT
