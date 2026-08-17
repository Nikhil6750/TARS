"""Backend configuration, sourced from environment variables / .env.

Field names and defaults mirror `.env.example` at the repo root exactly —
that file is the documented contract for what can be configured; this is
its typed runtime counterpart. Do not add a setting here without adding it
to `.env.example` as well.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- General ----
    tars_env: str = "development"
    # Explicit override for the bind address. Leave unset (None) to get the
    # secure-by-default behavior driven by `bind_lan` below — never publicly
    # exposed by default, per AGENTS.md.
    backend_host: str | None = None
    backend_port: int = 8000
    database_url: str = "sqlite:///./tars.db"
    cors_allow_origins: str = "*"

    # ---- Mock trading events ----
    use_mock_trading_events: bool = True
    mock_event_interval_seconds: float = 45.0

    # ---- quant_brain (future) ----
    quant_brain_base_url: str | None = None
    quant_brain_api_key: str | None = None

    # ---- Voice orchestration ----
    pipecat_transport: str = "webrtc"

    # ---- Wake word ----
    wake_word_provider: str = "mock"
    wake_word_phrase: str = "TARS"
    wake_word_model_path: str | None = None
    wake_word_threshold: float = 0.5

    # ---- VAD ----
    vad_provider: str = "silero"

    # ---- STT ----
    stt_provider: str = "mock"
    faster_whisper_model: str = "base"
    faster_whisper_device: str = "cpu"
    faster_whisper_compute_type: str = "int8"
    openai_api_key: str | None = None

    # ---- TTS ----
    tts_provider: str = "mock"
    fish_speech_model_path: str | None = None
    fish_speech_api_url: str = "http://localhost:8080"
    fish_speech_reference_id: str | None = None
    kokoro_model_path: str | None = None
    kokoro_voices_path: str | None = None
    kokoro_voice: str = "af_heart"
    kokoro_lang: str = "en-us"
    fish_audio_api_key: str | None = None

    # ---- Assistant ----
    assistant_provider: str = "mock"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str | None = None
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-5"
    claude_code_command: str = "claude"
    claude_code_timeout_seconds: float = 60.0
    # Chart analysis reads an image (Claude's own Read tool) on top of the
    # ordinary text turn, so it gets a longer allowance than chat replies.
    chart_analysis_timeout_seconds: float = 120.0

    # ---- Memory ----
    obsidian_vault_path: str = "./vault"
    sqlite_vec_enabled: bool = False
    embedding_model: str | None = None

    # ---- Scheduling ----
    tars_timezone: str = "UTC"

    # ---- Agent framework ----
    # SetupWatchAgent is a read-only, deterministic-state watcher (never
    # generates trade signals -- see agents/setup_watch_agent.py) that is
    # safe to run continuously by default; disable it if a quieter dev
    # environment is preferred.
    setup_watch_agent_enabled: bool = True
    setup_watch_agent_interval_seconds: float = 30.0

    # ---- Connectivity ----
    # Tailscale Serve is the preferred private path to reach this backend
    # from another device (e.g. iPhone) — it proxies a localhost-bound
    # service over the tailnet, so it works with the secure default below
    # without any LAN exposure. `tailscale_hostname` is informational only
    # here (surfaced by /api/v1/health-style tooling); actual `tailscale
    # serve` configuration happens outside this app. Tailscale Funnel
    # (public exposure) is never used for normal TARS operation — see
    # ARCHITECTURE.md § Connectivity / ADR-014.
    tailscale_hostname: str | None = None
    # False (default): bind loopback-only (127.0.0.1) — not reachable from
    # any other device, including on the same LAN. True: bind 0.0.0.0 so
    # other devices on the same LAN can reach it (e.g. for local dev on a
    # phone before Tailscale is set up). Does not, by itself, expose TARS
    # to the public internet — that requires separately port-forwarding or
    # using Tailscale Funnel, neither of which this app does automatically.
    bind_lan: bool = False

    # ---- Notifications ----
    web_push_vapid_public_key: str | None = None
    web_push_vapid_private_key: str | None = None

    # ---- Observability ----
    otel_exporter_otlp_endpoint: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None

    @property
    def sqlite_path(self) -> Path:
        raw = self.database_url
        prefix = "sqlite:///"
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
        path = Path(raw)
        if not path.is_absolute():
            path = REPO_ROOT / path
        return path

    @property
    def effective_host(self) -> str:
        """The actual bind address: an explicit `backend_host` always wins;
        otherwise secure-by-default (127.0.0.1) unless `bind_lan` opts in
        to 0.0.0.0. See the `bind_lan` field docstring above."""
        if self.backend_host:
            return self.backend_host
        return "0.0.0.0" if self.bind_lan else "127.0.0.1"

    @property
    def cors_origins(self) -> list[str]:
        raw = self.cors_allow_origins.strip()
        if raw == "*":
            return ["*"]
        return [origin.strip() for origin in raw.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
