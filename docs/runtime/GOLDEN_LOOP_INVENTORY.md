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
| Turn ownership and idempotency | `apps/backend/assistant/turn_controller.py` | `TarsOrchestrator`, `AssistantRouter`, `AssistantBridgeProcessor`, `VoiceAssistantRuntime` previously each owned part of execution | Implemented one controller with in-flight joining, completed replay, and conflict rejection. Orchestrator/router are downstream advanced executors only. |
| Voice ingestion | `POST /api/v1/voice/utterance` | `/voice/transcribe`, removed `/voice/session`, former native `/voice/transcribe` plus frontend execution | Native and PTT submit each WAV once to the canonical endpoint. Low-level transcribe/synthesize endpoints are deprecated diagnostics only; the independent WebSocket voice session was removed. |
| VAD/audio segmentation | `apps/web/src-tauri/src/wake_engine.rs` | Dormant Pipecat module; browser PTT capture; native energy VAD | Native capture/VAD is the desktop production path. Rust wake/chart regexes, command deadlines, and independent dispatch events were removed. Browser PTT posts to the same endpoint. |
| STT | Backend `SpeechToTextProvider` used by the controller | Native posts to `/voice/transcribe`; Pipecat has another segmented STT route | The canonical utterance endpoint invokes the configured backend provider exactly once per audio segment. |
| Wake recognition | Backend configurable transcript normalizer/matcher | Removed Rust `wake_regex` and removed browser `wake-word.ts`; optional openWakeWord adapter remains dormant | Backend transcript matching is authoritative. No production native/browser matcher remains. Do not claim openWakeWord readiness without a real model. |
| Voice state | Backend controller response/state contract | Rust transport state; React visual projection | Rust reports capture/transcription transport and maps the backend final state; React observes and projects only LISTENING/THINKING/SPEAKING/IDLE. It does not own command state. |
| Intent routing | Small deterministic `TurnIntentRouter` | Downstream orchestrator/assistant specialized routes; removed Rust and active React chart routing | Controller selects the top-level path. Existing specialized routers remain behind deterministic or advanced escalation and cannot receive a second copy of the turn. |
| Normal conversation | Direct configured conversational provider with failure-only fallback | Full orchestrator, memory/context, intelligence, provider capability/health routing and quality processing run on the ordinary path | Add `NORMAL_CONVERSATION_FAST_PATH`; no tools, research, chart, memory lookup, critic, or parallel providers by default. |
| Deterministic actions | Controller delegates once to deterministic assistant/action runtime | Assistant deterministic state queries, `skills.voice_bridge`, frontend HUD bypass, orchestrator fixed routes | Consolidate selection at the top-level router; preserve `ActionRuntime` and `PermissionEngine` for actual action execution. |
| Chart analysis | Controller checks `HotChartState`; native WGC watcher refreshes it independently | Specialized capture/analyze endpoints remain for explicit tooling, but Rust/React no longer detect chart intent | Preserved WGC watcher, state store, fast response, capture and async deep verification. Fresh state answers immediately; stale state fails transparently while the watcher refreshes instead of triggering UI-owned execution. |
| Assistant HTTP | `/api/v1/assistant/query` and its stream adapter delegate to controller | v1 aliases plus `/api/v2/assistant/query`, direct orchestrator calls | Remove v2 ambiguity. Keep legacy message aliases as deprecated adapters to the same controller while tests/clients migrate. |
| Streaming | Controller event stream with `state`, `delta`, and one final `AssistantResponse` | Former assistant/chart-specific UI streams and React-side sentence extraction/TTS | Text activity streams immediately through one controller shape. Voice audio is returned as complete backend-synthesized sentence chunks; React does not derive speech. |
| Response contract | `AssistantResponse` (`turn_id`, `display_text`, `speech_text`, `intent`, `status`, `provider`, `latency_ms`) | Frozen nested assistant-message plus v2 presentation envelope; chart-specific result shape | Add an internal/public V1 turn response without changing frozen `contracts/`. Legacy adapters may project it into old shapes. |
| Speech formatting | Backend `ResponseComposer`/sentence chunker | Removed React `composeSpeech` module and active chart-specific speech fallback | Backend produces final speech once. React displays or plays provided output only. |
| TTS | Controller invokes configured Kokoro provider once per complete response/chunk | `/voice/synthesize`, Pipecat TTS, React queue repeatedly calling synthesize | Controller owns synthesis handoff and stage telemetry. Diagnostic synthesize endpoint remains non-executing and deprecated. |
| TTS playback | Native/web audio transport plays backend-produced WAV chunks | React requests synthesis and manages business-state transitions | Playback may remain in the client transport, but it receives ready audio/speech events and never composes speech or re-executes a command. |
| Telemetry | One `VoiceTurnTrace` per `turn_id`, including transcript, wake match and exact stage failure | Split native telemetry IDs and independent backend transcribe/synthesize traces | Extend the durable backend trace and correlate all stages with the controller turn ID. Telemetry failures are non-blocking. |
| Memory | Optional enrichment selected by explicit advanced intent | Ordinary `AssistantRouter` always attempts memory search | Memory observes/persists all completed turns but does not block or automatically enrich normal conversation. |
| Startup | `scripts/start_tars.ps1` always builds and launches the worktree-local release binary | Former launcher could select shared Cargo binaries and continue against another worktree's port owner | Refuses a foreign port owner, initializes the backend/voice services, builds without Vite, launches only the worktree-local executable, and verifies a native window handle. |

## Preserve behind the controller

The rebuild must retain `HotChartState`, WGC capture, the chart watcher,
asynchronous deep verification, Claude Code/Codex providers, skill registry,
Obsidian memory, `ActionRuntime`, `PermissionEngine`, the read-only
`quant_brain` boundary, and research fail-closed behavior. None remains on the
ordinary conversation fast path unless the small intent router explicitly
selects it.
