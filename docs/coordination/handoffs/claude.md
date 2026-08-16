# Handoff — Claude Code

Owned directory: `apps/backend/`. Only Claude Code edits this file. See
[AGENTS.md](../../../AGENTS.md) for the full handoff protocol.

Update this file at the end of every session working on `apps/backend/`, using
the template below. Keep only the latest handoff at the top; older entries
may be kept below a `---` separator if useful for history, but are not
required reading for the next session (`CURRENT_STATE.md` is authoritative
for that).

---

## Latest handoff — Wave 1 backend implementation (2026-08-16)

**Branch**: `feature/v1-backend-voice`
**Base SHA**: `ad6f4bf6bd4dcb5c4039450dc8b8540ce63108e7` (tip of `integration/v1` at session start)
**Final SHA**: `2bd64f664bf659085c67d3660ca21931ea06305a`

**Commits** (each pushed after its own test/lint/type-check pass):

| SHA | Summary |
|---|---|
| `f1fc132` | Core event backend: FastAPI + WebSocket + SQLite (Phase A) |
| `88fd015` | Assistant provider abstraction + deterministic routing (Phase B) |
| `1d7fda0` | Memory/retrieval layer: SQLite FTS5 + Obsidian vault indexing (Phase C) |
| `df2a9e1` | Voice pipeline: Pipecat + local STT/TTS/wake-word providers (Phase D) |
| `bea3c14` | Voice/assistant API surface + secure-by-default networking (Phase E+F) |
| `2bd64f6` | OpenTelemetry instrumentation (ADR-017) |

All six commits are on `feature/v1-backend-voice` and pushed to
`origin/feature/v1-backend-voice`. Nothing merged into `integration/v1` or
`main` — that is a coordinator decision per `AGENTS.md`.

## Work completed

Implemented the full backend mission (Phases A–F) plus OpenTelemetry
instrumentation from `TASKS.md`:

- **Phase A — Core event backend.** FastAPI app (`app/`), SQLite schema +
  migration runner (`storage/`), mock trading-event generator
  (`events/generator.py`), deterministic active-state calculation
  (`events/service.py`), WebSocket broadcast (`/ws/events`). Every event is
  validated directly against `contracts/trading-event.schema.json` via
  `jsonschema` (`app/contracts.py`) — not a hand-duplicated copy — so the
  backend can never silently drift from the frozen contract.
- **Phase B — Assistant tool layer.** `AssistantProvider` interface with
  four adapters: `MockAssistantProvider`, `ClaudeCodeProvider` (headless
  `claude -p --output-format json` CLI invocation, verified against current
  Claude Code docs, not assumed), `OllamaProvider` (local HTTP), optional
  `AnthropicAPIProvider` (paid, lazy-imports `anthropic`). Deterministic
  routing (`assistant/router.py`) resolves "show active setups" / "what
  requires my attention" straight from `EventService` — these never reach a
  model call. Everything else gets deterministic active-setup state as
  grounding text instructing the model never to invent trading facts.
  Conversation turns persist to `conversation_messages`, validated against
  `contracts/assistant-message.schema.json`.
- **Phase C — Memory.** `MemoryService` (`memory/`) with SQLite FTS5 full-
  text search over conversation memory and an indexed Obsidian vault
  (read-only indexer, content-hash-gated, excludes `.obsidian`/`.git`).
  Every search result carries `source`/`source_id` for traceability.
  Per ADR-013, `MemoryService` refuses to start with
  `SQLITE_VEC_ENABLED=true` rather than silently ignoring it — no
  measurement has justified semantic retrieval over FTS5 yet. APScheduler
  wired for periodic vault reindexing (housekeeping only, per ADR-016).
- **Phase D — Voice.** `WakeWordProvider`, `SpeechToTextProvider`,
  `TextToSpeechProvider` interfaces, each with a zero-dependency mock plus
  real local adapters: `FasterWhisperSTTProvider`, `KokoroTTSProvider`
  (kokoro-onnx, fully local), `FishSpeechTTSProvider` (HTTP client against
  a separately-run local Fish Speech API server), `OpenWakeWordProvider`.
  The realtime pipeline (`voice/pipeline.py`) is built on pipecat-ai
  1.7.0's current `PipelineWorker`/`WorkerRunner` API — verified against
  the installed package's actual module structure via introspection, not
  assumed from training data, since 1.7.0 deprecated the
  `PipelineTask`/`PipelineRunner` API shown in older Pipecat examples.
  Generic `ProviderBridgeSTTService`/`ProviderBridgeTTSService` wrap our
  own provider instances rather than Pipecat's vendor-specific services, so
  the realtime session and the one-shot REST endpoints share exactly one
  adapter instance per provider. Silero VAD needs no separate package —
  bundled inside `pipecat-ai` as an ONNX model.
- **Phase E — Voice/assistant API.** `POST /api/v1/voice/transcribe`
  (WAV → text), `POST /api/v1/voice/synthesize` (text → WAV),
  `GET /api/v1/voice/status` (provider readiness/names, never secrets),
  `WS /api/v1/voice/session` (realtime audio↔audio over the Phase D
  pipeline). Text question/response already existed from Phase B
  (`POST /api/v1/assistant/query`). STT/TTS/wake-word providers load in a
  background task (`app/voice_state.py`) so cold model downloads (minutes,
  see Benchmarks below) never block app startup; health/events/assistant
  work immediately.
- **Phase F — Local network.** `Settings.effective_host` resolves to
  `127.0.0.1` by default; `BIND_LAN=true` opts into `0.0.0.0`; an explicit
  `BACKEND_HOST` always wins. Tailscale Serve (the preferred private
  remote-access path, ADR-014) works against the loopback default without
  needing LAN exposure. `CORS_ALLOW_ORIGINS` is configurable (default `*`).
  `run.py` is the settings-aware uvicorn entrypoint. Verified with a real
  server smoke test: bound `127.0.0.1:8000`, `/api/v1/health` and
  `/api/v1/voice/status` both responded correctly.
- **OpenTelemetry (ADR-017).** `app/observability.py` configures a
  `TracerProvider` that logs spans through the stdlib logger by default (no
  network dependency) or exports to OTLP if `OTEL_EXPORTER_OTLP_ENDPOINT` is
  set. Instruments event ingestion, assistant requests (provider, latency,
  errors), and memory retrieval (retrieved `source:source_id`s). Verified
  end-to-end — spans logged correctly with no secrets in any attribute.

## Files changed

~80 files under `apps/backend/` (new: `app/`, `storage/`, `events/`,
`assistant/`, `memory/`, `voice/`, `tests/`, `run.py`, `README.md`,
`requirements*.txt`) plus additive edits to `.env.example` (root-level
config template — not under `docs/coordination/` or `contracts/`, and
already owns the backend env vars this session extended; edited
additively only, no existing key removed or renamed without a documented
reason). See the six commits above for the exact diff per phase.

## Interfaces exposed

**HTTP** (all under `/api/v1`):
- `GET /health`
- `GET /events`, `GET /events/active`, `POST /dev/mock-event`
- `POST /assistant/query`
- `GET /memory/search`, `POST /memory/reindex-vault`
- `POST /voice/transcribe`, `POST /voice/synthesize`, `GET /voice/status`

**WebSocket**:
- `/ws/events` — trading-event + active-setup broadcast
- `/api/v1/voice/session` — realtime voice (Pipecat pipeline)

**Python interfaces** (for future backend work, not external consumers):
`assistant.provider.AssistantProvider`, `voice.interfaces.WakeWordProvider`
/ `SpeechToTextProvider` / `TextToSpeechProvider`, `memory.service.MemoryService`.

## Environment/configuration

All new settings are documented in `.env.example` at the repo root and
`apps/backend/app/config.py` (kept in lockstep per that file's own
docstring rule). Defaults are the free/local-first path:
`ASSISTANT_PROVIDER=claude_code`, `STT_PROVIDER=faster_whisper`,
`TTS_PROVIDER=kokoro`, `WAKE_WORD_PROVIDER=mock` (see Known limitations),
`BIND_LAN=false`. Two new requirements files: `requirements-voice.txt`
(pipecat-ai + local model packages — optional, every voice provider mocks
without it) and `requirements-optional.txt` (paid-provider SDKs — never
required).

## Benchmarks (Phase D)

Measured in this sandboxed dev environment (CPU-only, no GPU):

| Adapter | Cold init (incl. first-run download) | Warm init | Generation latency |
|---|---|---|---|
| faster-whisper (`tiny`) | 48.98s | ~2.3s | 0.70s for 1s of audio |
| Kokoro TTS | 187.97s | ~2.5s | 8.66s cold / 0.77s warm, for a one-sentence reply |
| Fish Speech (local) | **not benchmarked** — see below | — | — |

**Fish Speech could not be benchmarked in this environment.** It has no
pip-installable inference API; running it requires cloning the
`fish-speech` repo, downloading GB-scale checkpoints, and running a
separate local API server (`tools/api_server.py`) this sandbox had no
practical way to host. `FishSpeechTTSProvider` is implemented as a
best-effort HTTP client against that server's documented `/v1/tts`
endpoint, but its exact request/response JSON schema was not fully
published where I could reach it — verify against the actual deployed
server version before relying on it. **Kokoro is the default `TTS_PROVIDER`
in `.env.example`** on this evidence: fully local, no separate process, and
fast once warm. If Fish Speech is benchmarked on real target hardware
(ideally with a GPU) and found to sound meaningfully better, flipping the
default is a one-line `.env` change — the provider is fully implemented.

## Tests

63 pytest cases across `apps/backend/tests/`, all passing; `ruff check .`
and `mypy app events storage assistant memory voice run.py` both clean.
Covers: contract validation, event persistence/active-state/invalidation,
WebSocket broadcast, deterministic assistant routing (proven to never call
the configured provider), assistant provider fallback-to-mock on
misconfiguration, memory FTS5 round-trip + vault indexing lifecycle, voice
mock providers, audio WAV↔PCM16 conversion, voice provider factory error
paths, network config resolution, and — the only tests touching real ML
models rather than mocks — `tests/test_voice_real_adapters.py`, which
exercises faster-whisper and Kokoro against actual (already-cached in this
environment) model weights; these skip cleanly via `pytest.importorskip`
if the voice extras aren't installed, so the rest of the suite needs no
network and no API keys.

**Not run**: no live audio round-trip through `/api/v1/voice/session` —
that needs a real microphone/browser client, which Antigravity owns. The
pipeline was verified by introspecting the installed `pipecat-ai` 1.7.0
package's real API (class names, constructor signatures, method
signatures) rather than assumed, and by the unit-level construction tests,
but not by an actual end-to-end voice conversation.

## Known limitations

- **No custom "TARS" wake-word model.** openWakeWord ships no pretrained
  model for the phrase "TARS" (only generic phrases like "hey jarvis").
  `OpenWakeWordProvider` is fully implemented and will run any
  custom-trained model pointed at by `WAKE_WORD_MODEL_PATH`, but training
  one is a separate, later effort (openWakeWord's own training pipeline,
  requiring assembled audio samples). `.env.example` defaults
  `WAKE_WORD_PROVIDER=mock` for this reason. Push-to-talk is unaffected —
  `/api/v1/voice/session` never requires wake-word detection to begin a
  session, satisfying AGENTS.md's "guaranteed regardless of wake-word
  state" requirement structurally.
- **Fish Speech not benchmarked** — see Benchmarks above.
- **Wake word is not wired into the realtime pipeline as a gate.** The
  `WakeWordProvider` interface and `OpenWakeWordProvider` adapter are
  implemented and unit-testable, but `voice/pipeline.py`'s realtime session
  doesn't yet use them to gate when STT starts listening — the pipeline is
  push-to-talk/continuous-session by design (the client decides when to
  open the WebSocket). Wiring wake-word detection as an always-on gate
  ahead of a real trained model existing seemed like the wrong order of
  operations; flagging it as intentionally deferred, not an oversight.
- **No live voice round-trip test** — see Tests above.
- **Memory search is not wired into assistant grounding.** Phase C built a
  complete, working retrieval service (FTS5 + vault + API), but Phase B's
  `AssistantRouter` only grounds on deterministic trading state, not on
  retrieved memory/vault context. Wiring "recall relevant past notes into
  the assistant's context" is a natural next step, deliberately not done
  here to avoid scope creep into a Phase B change under a Phase C commit.
- **`fastapi`/`starlette` were upgraded** from `0.115.0`/(implicit) to
  `0.141.1`/`1.6.0` mid-session — `pipecat-ai[websocket]` pulled in the
  newer versions as a transitive dependency, and I re-pinned
  `requirements.txt` to match after confirming the full test suite still
  passes at the new versions. Flagging this since it's outside the diff
  Phase A originally shipped.
- **Python 3.13 was used**, not the 3.12 named in `AGENTS.md`/`ARCHITECTURE.md`
  — this machine only had 3.13 and 3.7 available (`py -0` confirmed no 3.12
  install). Nothing in the codebase is 3.13-specific; it should run
  unmodified on 3.12 if that's later required, but this wasn't verified on
  3.12 in this session.

## Exact dependencies required from other agents

- **Antigravity (frontend)**: the WebSocket/HTTP contracts above are
  stable and ready to consume. For voice specifically:
  `/api/v1/voice/session` expects 16-bit PCM mono audio in and returns the
  same out (no WAV wrapping over the wire — `add_wav_header=False`); WAV
  wrapping is only used by the one-shot `/api/v1/voice/transcribe`
  (upload) and `/api/v1/voice/synthesize` (response) endpoints. Browser
  `MediaRecorder` typically produces webm/opus, not raw PCM — the frontend
  is responsible for any needed transcoding before hitting these
  endpoints; this backend does not transcode.
- **Codex (tests/tools)**: `apps/backend/README.md` documents how to run
  the test suite and what's expected to work with zero API keys (the
  `mock` provider path for assistant/STT/TTS/wake-word — confirmed via
  `tests/test_assistant_provider_fallback.py` and the voice mock tests).
  The `contracts/*.schema.json` validation lives in `app/contracts.py`,
  loading the canonical files directly (no forked copy) — useful if Codex
  wants to cross-check contract conformance independently.

## Next recommended action

1. A coordinator decides whether/when to merge `feature/v1-backend-voice`
   into `integration/v1`, per `AGENTS.md`'s merge-strategy note in
   `TASKS.md` (Cross-cutting / Post-Wave-1).
2. If someone has GPU hardware available, benchmark Fish Speech properly
   (run the local API server, compare against the Kokoro numbers above)
   before treating the current `TTS_PROVIDER=kokoro` default as final.
3. Training a real "TARS" openWakeWord model, if always-on wake-word
   listening (vs. push-to-talk) becomes a priority.
4. A live voice round-trip test once Antigravity has a client that can
   open `/api/v1/voice/session` with a real microphone.
5. Wiring memory search into assistant grounding (Known limitations above)
   if "recall past notes" becomes a desired assistant capability.
