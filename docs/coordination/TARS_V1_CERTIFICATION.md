# TARS V1 Certification

## Verdict

**NOT CERTIFIED**

Target branch: `feature/v1-integration`

Target SHA: `e8308d82361e6df3c0928c4994ac0505a2e4235f`

Certification branch: `cert/v1-integration-e8308d8`

Certification date: 2026-08-16

This is an independent rerun and source/runtime review of the exact target SHA. Prior
integration reports were treated as claims, not evidence. No product code was changed.

## Repository identity

- Initial worktree: clean.
- Initial branch: `feature/v1-integration`.
- Initial `HEAD`: exact target SHA.
- Source SHAs `5f5a1fb` (backend), `2c67932` (quality), and `5d677db` (web) are all
  ancestors of the target.
- `main` remained at `bd7b745927a35b7b2ae11d2facae3ac6a685e1e7`, matching
  `origin/main`.
- `integration/v1` remained at `ad6f4bf6bd4dcb5c4039450dc8b8540ce63108e7`, matching
  `origin/integration/v1`.
- The two canonical contract blobs are byte-identical to the integration baseline.

## Certification summary

| Area | Result | Evidence |
|---|---|---|
| Backend | **FAIL** | 61 passed, 1 skipped; configured MyPy passed; Ruff failed with two `E402` violations in `apps/backend/run.py`. |
| Contracts | **PASS** | 40 non-live quality tests passed, 15 acceptance tests skipped as designed outside the harness; codegen drift check passed; canonical schemas unchanged. |
| Frontend | **FAIL** | 12 Vitest tests, TypeScript, and production build passed, but the real UI does not consume the backend's live event envelope. No lint script is configured. |
| Acceptance harness | **PASS, insufficient for release** | Fresh real-process run passed 15/15, but it does not test the backend envelope through the actual frontend client, genuine voice, historical preservation, chunked upload limits, or Tauri. |
| Voice | **FAIL** | The frontend records no audio bytes, never calls transcription, sends hard-coded text, and uses browser speech synthesis instead of backend TTS. |
| Memory | **FAIL** | Vault -> FTS5 -> retrieval -> assistant context works, but source IDs are dropped from assistant grounding and the UI displays fabricated performance samples. |
| WebSocket | **FAIL end-to-end** | Backend multi-client lifecycle behavior passed, but backend `event` and frontend `payload` envelopes disagree; the browser received the frame and did not update. |
| Tauri | **NOT VERIFIED / release FAIL** | Source is present, but native build failed because Rust/Cargo/MSVC are absent; `tauri info` also reports mismatched JS/Rust package minor versions. No native feature was executed. |
| PWA | **PASS with voice limitation** | Production manifest and active service worker verified; 390x844 and 1440x900 rendered without overflow. The UI correctly disclaims background iOS wake-word support. |
| Security | **FAIL** | Standard oversized request, malformed input, secret reflection, unknown fields, and execution-surface checks passed; a >1 MiB chunked request bypassed the body limit and returned 201. |

## Validation rerun

Commands were run fresh from the target SHA.

- `python -m pytest apps/backend/tests -q`: **61 passed, 1 skipped**, one warning.
- `python -m ruff check apps/backend`: **FAIL**, two `E402` errors at
  `apps/backend/run.py:10` and `apps/backend/run.py:12`.
- `python -m mypy .` from `apps/backend`: **PASS**, 56 source files.
- `python -m pytest tests -q`: **40 passed, 15 skipped**. The 15 are the live
  acceptance tests and are enabled by the harness.
- `python tools/generate_contracts.py --check`: **PASS**, no drift.
- `npm test`: **12 passed** across three Vitest files.
- `npm run typecheck`: **PASS**.
- Frontend lint: **not run; no lint script/configuration exists**.
- `npm run build`: **PASS**; production assets, web manifest, service worker, and
  Workbox bundle were generated.
- `python tools/run_acceptance.py ...`: **15 passed** against processes launched by
  the harness with paid keys removed and an isolated database.
- `npm audit --audit-level=high`: **PASS**, zero vulnerabilities.
- `python -m pip check`: **FAIL** in the machine's global Python environment because
  `ddgs 9.14.4` requires `httpx>=0.28.1` while `httpx 0.27.2` is installed.

The first MyPy invocation from repository root did not load
`apps/backend/pyproject.toml` and is not used as the configured result. The rerun from
`apps/backend` loaded the checked-in configuration and passed.

## Coverage and backend test-count explanation

No backend test file changed between `5f5a1fb` and the integration target. The reported
delta is environmental, not an integration deletion:

- Claude's environment executed 63 tests, including two real-model cases in
  `test_voice_real_adapters.py`.
- This certification environment has neither `faster_whisper` nor `kokoro_onnx`
  installed. The test module calls `pytest.importorskip` at import time, producing one
  skipped module and leaving 61 executed tests.
- The skip is defensible for a base environment where voice extras are optional, but
  genuine faster-whisper and Kokoro behavior is consequently **unverified here**.
- The skip policy can also hide provider/model initialization failures by converting
  them to skips.
- The acceptance case named `test_voice_reports_real_local_provider_path` checks only
  that provider names occur in the status payload's `supported_providers` list. The
  harness deliberately configures active providers to `mock`, so that case does not
  prove a real STT/TTS path.

The acceptance fixture's expiry adjustment and Windows process-launch fixes are
reasonable integration changes and did not weaken assertions. The material weakness is
missing coverage of the actual frontend/backend envelope, historical preservation,
real voice, and chunked upload behavior.

## Voice certification

Classification: **FAIL**.

Backend provider interfaces, single-shot transcription/synthesis routes, and a Pipecat
pipeline are implemented. Mock endpoint tests pass. They do not form a genuine product
voice path in the integrated UI:

- `apps/web/src/services/audio.ts` opens the microphone only for an analyser/volume
  meter. It creates no `MediaRecorder`, PCM buffer, WAV, upload, or voice WebSocket.
- On PTT release, `apps/web/src/App.tsx:261` substitutes the literal
  `Show active setups` and posts it directly to `/api/v1/assistant/query`.
- A browser run with a fake microphone confirmed the PTT entered `LISTENING`, then sent
  exactly that hard-coded assistant request. It made **zero** calls to
  `/voice/transcribe` and `/voice/synthesize`.
- Assistant responses are spoken with browser `window.speechSynthesis`, not the
  backend/local TTS provider.
- `onVoiceTranscribed` is declared/passed but is not consumed by `VoiceControlView`.
- `faster_whisper`, `kokoro_onnx`, `pipecat`, and `openwakeword` are not installed in
  this environment. Cached Kokoro model files exist, but executable adapters do not.
- Fish Speech was not run against a real local server and remains **UNVERIFIED**.

Therefore the requested microphone -> STT -> transcription -> assistant -> response ->
backend TTS -> playable audio chain does not currently exist from the product UI.

## Event lifecycle and history

Classification: **BLOCKER**.

The backend invalidation endpoint reuses the original `event_id`, and persistence uses
`INSERT OR REPLACE INTO trading_events` (`apps/backend/events/service.py:34`). The
invalidation route explicitly constructs the terminal event with the original ID
(`apps/backend/app/routers/events.py:97`). Because `event_id` is the history table's
primary key, the invalidated row replaces the valid row.

An independent bounded lifecycle run produced:

```text
history_states=[(<valid event id>, SETUP_INVALIDATED),
                (<developing event id>, SETUP_DEVELOPING)]
valid_history_preserved=False
invalidated_history_preserved=True
```

Active state cleared correctly, both clients received transitions, and repeated
connections were deterministic. However, `SETUP_VALID` ceased to be historically
observable. This violates the required event-history semantics and is a release
blocker.

The acceptance lifecycle test checks that the valid event exists before invalidation
and that active state later clears; it never checks that both valid and invalidated
history records coexist afterward.

## Contract and API review

- Canonical schemas: unchanged; code generation matches.
- Route aliases map to the same handlers. Frontend HTTP and configured WebSocket routes
  exist in the backend.
- Canonical event ingestion rejects unknown fields; malformed JSON returns 422.
- The standard Content-Length oversized case returns 413.
- **Blocker:** the middleware checks only the `Content-Length` header. A streamed
  1,049,857-byte JSON event sent with chunked transfer encoding had no Content-Length,
  bypassed the limit, persisted, and returned HTTP 201.
- Acceptance confirmed its tested assistant errors do not reflect the secret sentinel.
- OpenAPI and explicit route probes found no trade/order execution route.

## Memory and grounding

The backend path is substantially implemented. A temporary Obsidian-compatible vault
was indexed at startup, FTS5 returned the note with
`source=vault` and `source_id=Research/cert-note.md`, and a capture provider confirmed
the vault snippet reached assistant system context. A missing-data query through the
fresh acceptance harness returned no fabricated trade facts.

Two release problems remain:

1. `build_system_context` includes each note's `source` and `snippet`, but drops
   `source_id`. The independent capture showed
   `vault_source_id_grounded=False`, so the assistant cannot retain/cite the exact
   source identifier even though FTS5 returned it. The frontend also reads `r.path`
   instead of `r.source_id`, losing the displayed vault path.
2. `apps/web/src/components/memory/MemoryView.tsx` falls back to hard-coded sample
   memory whenever there are no backend results. One sample claims a verified
   five-year walk-forward DSR and realized Sharpe for a nonexistent quant_brain record
   (`MemoryView.tsx:32`). This is fabricated strategy performance and directly violates
   the permanent product boundary. It is a **BLOCKER**.

## WebSocket end-to-end review

Backend-only bounded verification passed:

- two independent clients received `SETUP_DEVELOPING`, `SETUP_VALID`, and
  `SETUP_INVALIDATED`;
- a newly connected client received the correct active snapshot;
- invalidation cleared active state for all clients;
- ping/pong worked;
- malformed client JSON was ignored without killing the connection;
- reconnect after invalidation received an empty active snapshot.

The actual frontend/backend integration fails. Backend broadcasts:

```json
{"type":"trading_event","event":{...}}
```

The frontend only unwraps `msg.payload` for a `trading_event`
(`apps/web/src/services/websocket.ts:164`) and otherwise tries to validate the wrapper
itself. Its unit test supplies a direct event and never tests the backend envelope.

A real browser connected to the backend, received a valid `CERTLIVE` broadcast frame,
and the backend active endpoint contained it, but the UI still did not display the
symbol. This is a **BLOCKER** and explains why the generic external clients passed while
the product UI did not.

## Tauri / Windows status

| Capability | Implemented in source | Executed | Verified |
|---|---:|---:|---:|
| Shell/config | Yes | No | No |
| Compact resize | Yes | No | No |
| Always-on-top toggle | Yes | No | No |
| Notification plugin/API | Yes | No | No |
| Tray | Icon/config only; no tray menu/event behavior found | No | No |

`npm run tauri -- build` failed before compilation because `cargo` is not installed.
`tauri info` also found no Rust toolchain, MSVC, or Windows SDK and reported mismatched
Tauri minor versions between JavaScript packages and Rust crates. Native launch was not
possible. Source presence is not treated as runtime evidence.

## PWA status

- Production build generated `manifest.webmanifest`, `registerSW.js`, `sw.js`, and a
  Workbox bundle.
- Production preview exposed an installable manifest named `TARS Trading Companion`
  with `display=standalone`.
- Chromium reported an active service worker for the app scope.
- iPhone 390x844 and desktop 1440x900 viewports rendered meaningful content without
  horizontal overflow or an error overlay when checked with the app processes as
  appropriate.
- The UI explicitly states that continuous background wake-word listening is not
  supported on backgrounded/locked iOS; no contrary implementation or claim was found.
- PWA packaging does not cure the failed foreground voice pipeline described above.

The required browser verification was performed with the repository's pinned
Playwright tooling because the optional `agent-browser` executable was not installed.

## Blockers

1. Invalidation overwrites and destroys the prior `SETUP_VALID` history row.
2. Backend live-event envelope and frontend consumer disagree, so the real UI misses
   broadcasts.
3. Push-to-talk substitutes hard-coded text and never sends recorded speech to backend
   STT; playback bypasses backend TTS.
4. The Memory UI fabricates quant_brain performance facts (DSR and Sharpe).
5. The 1 MiB ceiling is bypassable with chunked transfer encoding.
6. The required Windows/Tauri artifact was neither buildable nor runnable in this
   environment and has package-version mismatch warnings.

## Warnings

- Real faster-whisper, Kokoro, openWakeWord, Pipecat, and Fish Speech execution remain
  unverified in this environment.
- Python 3.13.3 was used; the architecture specifies Python 3.12, which is not installed
  here.
- Ruff is not clean.
- No frontend lint target is configured.
- Global `pip check` is not clean because of the installed `ddgs`/`httpx` conflict.
- Tauri tray behavior is only declarative icon configuration, not an executed tray UX.

## Final verdict

**NOT CERTIFIED**

The passing unit/build/acceptance results establish a useful foundation, but they do
not outweigh the confirmed loss of historical evidence, broken real UI event delivery,
nonfunctional voice path, fabricated performance content, upload-limit bypass, and
unverified Windows desktop artifact. The exact target SHA is not ready to become the V1
baseline.
