# TARS minimal golden-loop runtime inventory

Base SHA: `eeb117a4c8474f7da82c6229b0dcf8f453525225`

This inventory records the production paths found before the minimal golden-loop
rebuild. It is descriptive evidence, not a second architecture specification.
The target is one backend-owned `AssistantTurnController`; native Rust owns
capture/segmentation and playback transport, while React only renders events and
submits explicit text/PTT input.

## Reference findings

- `Avinashb722/jarvis-ai-assistant` keeps its reliable basic path linear:
  microphone capture and recognition return one text value, one command
  dispatcher handles it, and that dispatcher produces speech. Its feature set is
  broad, but the ordinary turn is not split between UI and backend owners.
- `open-jarvis/OpenJarvis` makes the ordinary path explicit. `SimpleAgent` is a
  single `input -> engine -> AgentResult` turn with no tool calling, and the
  WebSocket chat path streams directly from one engine. Agent/tools mode is an
  explicit branch rather than mandatory middleware for every query.
- TARS should copy those ownership properties, not their implementation code:
  one turn owner, deterministic routing before model work, direct conversation
  by default, and explicit escalation into tools/research.

Reference commits inspected on 2026-08-22:

- `Avinashb722/jarvis-ai-assistant`: `aa291fd2cf49482b04cf584dabbb50a49b5301f7`
- `open-jarvis/OpenJarvis`: `759bfd0b98e9f0d391aa8482ca7b3c7dbd5bceea`

## Competing-path inventory and disposition

| Feature | Canonical implementation after rebuild | Existing/competing implementation | Action |
|---|---|---|---|
| Turn ownership and idempotency | `apps/backend/assistant/turn_controller.py` | `TarsOrchestrator`, `AssistantRouter`, `AssistantBridgeProcessor`, `VoiceAssistantRuntime` each own part of execution; no shared turn-id replay guard | Add one controller and make API/native compatibility paths delegate to it. Preserve orchestrator/router only as downstream advanced executors. |
| Voice ingestion | `POST /api/v1/voice/utterance` | `/voice/transcribe`, `/voice/session`, native `/voice/transcribe` followed by frontend assistant calls | Native submits each VAD segment once to the canonical endpoint. Keep low-level endpoints only as deprecated diagnostics/adapters; they cannot execute assistant turns independently. |
| VAD/audio segmentation | `apps/web/src-tauri/src/wake_engine.rs` | Pipecat/Silero VAD in `/voice/session`; browser PTT capture; native energy VAD | Keep stable native capture/VAD for the desktop golden loop. Remove command/wake/provider business decisions from Rust. Pipecat remains an optional compatibility transport that delegates completed transcripts to the controller. |
| STT | Backend `SpeechToTextProvider` used by the controller | Native posts to `/voice/transcribe`; Pipecat has another segmented STT route | The canonical utterance endpoint invokes the configured backend provider exactly once per audio segment. |
| Wake recognition | Backend configurable transcript normalizer/matcher | Rust `wake_regex`; browser `wake-word.ts`; optional openWakeWord provider with no guaranteed trained model | Backend transcript matching is authoritative. Native/browser matchers are deprecated for production execution. Do not claim openWakeWord readiness without a real model. |
| Voice state | Backend controller state and emitted turn events | Rust `WakeState`; React `CompanionVisualState`; `VoiceAssistantRuntime` transitions | Backend state is authoritative. Rust forwards capture observations; React maps backend states to four visual states only. |
| Intent routing | Small deterministic `TurnIntentRouter` | `TarsOrchestrator._route`, `AssistantRouter._try_deterministic`, `IntelligenceRouter`, Rust chart regex, React chart regex, provider task classifier | New router selects the top-level path. Existing routers remain behind TOOL/RESEARCH/TRADING_RESEARCH escalation and cannot receive a second copy of the turn. |
| Normal conversation | Direct configured conversational provider with failure-only fallback | Full orchestrator, memory/context, intelligence, provider capability/health routing and quality processing run on the ordinary path | Add `NORMAL_CONVERSATION_FAST_PATH`; no tools, research, chart, memory lookup, critic, or parallel providers by default. |
| Deterministic actions | Controller delegates once to deterministic assistant/action runtime | Assistant deterministic state queries, `skills.voice_bridge`, frontend HUD bypass, orchestrator fixed routes | Consolidate selection at the top-level router; preserve `ActionRuntime` and `PermissionEngine` for actual action execution. |
| Chart analysis | Controller checks `HotChartState` first, then requests the existing capture/analyze path | Separate `/assistant/analyze-chart`, `/assistant/analyze-chart/stream`, Rust chart detection, React `ChartAnalysisClient` | Preserve WGC watcher, state store, fast response, capture and async deep verification. Controller is the only intent owner; capture is transport, not UI reasoning. |
| Assistant HTTP | `/api/v1/assistant/query` and its stream adapter delegate to controller | v1 aliases plus `/api/v2/assistant/query`, direct orchestrator calls | Remove v2 ambiguity. Keep legacy message aliases as deprecated adapters to the same controller while tests/clients migrate. |
| Streaming | Controller event stream with `state`, `delta`, complete sentence `speech_chunk`, and final response | Assistant SSE, chart SSE, React-side sentence extraction/TTS | One backend stream shape. Speech sentence boundaries and sanitization happen once in backend. |
| Response contract | `AssistantResponse` (`turn_id`, `display_text`, `speech_text`, `intent`, `status`, `provider`, `latency_ms`) | Frozen nested assistant-message plus v2 presentation envelope; chart-specific result shape | Add an internal/public V1 turn response without changing frozen `contracts/`. Legacy adapters may project it into old shapes. |
| Speech formatting | Backend `ResponseComposer`/sentence chunker | Backend composer plus React `composeSpeech` fallback and chart-specific speech generation | Backend produces final speech once. React displays or plays provided output only; no sanitizer fallback. |
| TTS | Controller invokes configured Kokoro provider once per complete response/chunk | `/voice/synthesize`, Pipecat TTS, React queue repeatedly calling synthesize | Controller owns synthesis handoff and stage telemetry. Diagnostic synthesize endpoint remains non-executing and deprecated. |
| TTS playback | Native/web audio transport plays backend-produced WAV chunks | React requests synthesis and manages business-state transitions | Playback may remain in the client transport, but it receives ready audio/speech events and never composes speech or re-executes a command. |
| Telemetry | One `VoiceTurnTrace` per `turn_id`, including transcript, wake match and exact stage failure | Split native telemetry IDs and independent backend transcribe/synthesize traces | Extend the durable backend trace and correlate all stages with the controller turn ID. Telemetry failures are non-blocking. |
| Memory | Optional enrichment selected by explicit advanced intent | Ordinary `AssistantRouter` always attempts memory search | Memory observes/persists all completed turns but does not block or automatically enrich normal conversation. |
| Startup | `scripts/start_tars.ps1` verifies source-matched native build, backend voice readiness and loaded webview | Launcher can select shared Cargo binaries and reports process/window health with incomplete provenance | Build or reject stale binaries, start one backend, launch the source-matched native app, and verify the real UI readiness marker. |

## Preserve behind the controller

The rebuild must retain `HotChartState`, WGC capture, the chart watcher,
asynchronous deep verification, Claude Code/Codex providers, skill registry,
Obsidian memory, `ActionRuntime`, `PermissionEngine`, the read-only
`quant_brain` boundary, and research fail-closed behavior. None remains on the
ordinary conversation fast path unless the small intent router explicitly
selects it.
