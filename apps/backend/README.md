# TARS Backend

FastAPI + WebSocket + SQLite backend for TARS — event core, assistant
routing, memory/retrieval, and the realtime voice pipeline. See
[docs/coordination/ARCHITECTURE.md](../../docs/coordination/ARCHITECTURE.md)
for the full system design; this file covers running and configuring this
directory specifically.

## Setup

```sh
cd apps/backend
python -m venv .venv
.venv/Scripts/activate        # Windows; source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp ../../.env.example ../../.env   # then edit .env as needed
```

The core path (events, assistant routing with `mock`/`ollama`/
`claude_code`, memory/FTS5) needs nothing beyond `requirements.txt` — no
API keys, no downloaded models. Voice (real STT/TTS/wake-word, the
realtime Pipecat pipeline) needs the additional local model stack:

```sh
pip install -r requirements-voice.txt
```

Optional paid providers (`AnthropicAPIProvider`) live in
`requirements-optional.txt` — never required.

## Running

```sh
python run.py
```

Reads `BACKEND_HOST`/`BACKEND_PORT`/`BIND_LAN` from `.env` (see
Connectivity below) rather than taking them as CLI flags, so `.env` stays
the single source of truth for how the server binds. Equivalent to
`uvicorn app.main:app --host <effective_host> --port <port>`.

## Testing

```sh
pytest
ruff check .
mypy app events storage assistant memory voice
```

Model-backed voice tests (`tests/test_voice_real_adapters.py`) skip
cleanly if `requirements-voice.txt` isn't installed or a model can't be
fetched — the rest of the suite requires no network and no API keys.

## Connectivity

TARS is never publicly exposed by default. Three supported modes:

| Mode | Config | Reachable from |
|---|---|---|
| Localhost (default) | `BIND_LAN=false`, `BACKEND_HOST` blank | This machine only |
| LAN development | `BIND_LAN=true` | Other devices on the same LAN/WiFi |
| Tailscale Serve (preferred remote access, e.g. laptop → iPhone) | `BIND_LAN=false`, run `tailscale serve` against `127.0.0.1:<port>` | Your own tailnet only, over HTTPS |

Never port-forward this port on your router, and never use Tailscale
Funnel for normal operation (see
[DECISIONS.md ADR-014](../../docs/coordination/DECISIONS.md)) — Funnel
exposes the service to the public internet, which TARS's trading-related
data should never be.

`CORS_ALLOW_ORIGINS` defaults to `*` (fine for a single-user local
companion talking to its own frontend); set a comma-separated list to
restrict it once you know your frontend's exact origin(s).

## Directory layout

```
app/        FastAPI app, routers, config, DB, WebSocket manager
storage/    SQLite migrations + migration runner
events/     Mock trading-event generator + event persistence/active-state
assistant/  AssistantProvider interface + adapters + deterministic router
memory/     SQLite FTS5 + Obsidian vault indexing + retrieval service
voice/      WakeWordProvider/SpeechToTextProvider/TextToSpeechProvider
            interfaces + adapters + the realtime Pipecat pipeline
tests/      pytest suite, mirrors the packages above
```
