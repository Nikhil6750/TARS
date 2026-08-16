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
    backend_host: str = "0.0.0.0"
    backend_port: int = 8000
    database_url: str = "sqlite:///./tars.db"

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

    # ---- VAD ----
    vad_provider: str = "silero"

    # ---- STT ----
    stt_provider: str = "mock"
    faster_whisper_model: str = "base"
    openai_api_key: str | None = None

    # ---- TTS ----
    tts_provider: str = "mock"
    fish_speech_model_path: str | None = None
    kokoro_model_path: str | None = None
    fish_audio_api_key: str | None = None

    # ---- Assistant ----
    assistant_provider: str = "mock"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str | None = None
    anthropic_api_key: str | None = None
    claude_code_command: str = "claude"
    claude_code_timeout_seconds: float = 60.0

    # ---- Memory ----
    obsidian_vault_path: str = "./vault"
    sqlite_vec_enabled: bool = False
    embedding_model: str | None = None

    # ---- Scheduling ----
    tars_timezone: str = "UTC"

    # ---- Connectivity ----
    tailscale_hostname: str | None = None
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
