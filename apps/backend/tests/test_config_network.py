from __future__ import annotations

from app.config import Settings


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_default_effective_host_is_loopback_only():
    settings = _settings()
    assert settings.effective_host == "127.0.0.1"


def test_bind_lan_true_uses_0_0_0_0():
    settings = _settings(bind_lan=True)
    assert settings.effective_host == "0.0.0.0"


def test_explicit_backend_host_overrides_bind_lan():
    settings = _settings(bind_lan=True, backend_host="10.0.0.5")
    assert settings.effective_host == "10.0.0.5"


def test_cors_origins_wildcard():
    settings = _settings(cors_allow_origins="*")
    assert settings.cors_origins == ["*"]


def test_cors_origins_comma_separated_list():
    settings = _settings(cors_allow_origins="http://localhost:5173, http://100.64.0.1:5173")
    assert settings.cors_origins == ["http://localhost:5173", "http://100.64.0.1:5173"]


def test_backend_reload_defaults_false_even_in_development():
    """Regression test: reload must NOT be implicitly driven by tars_env.
    Uvicorn's --reload spawns requests in a separate Windows child worker
    that can't run asyncio.create_subprocess_exec, breaking ClaudeCodeProvider
    entirely -- tars_env=="development" (the normal launcher's default)
    previously turned reload on implicitly and broke every Claude call."""
    settings = _settings(tars_env="development")
    assert settings.tars_backend_reload is False


def test_backend_reload_true_requires_explicit_opt_in():
    settings = _settings(tars_backend_reload=True)
    assert settings.tars_backend_reload is True


def test_backend_reload_env_var_is_read_independently_of_tars_env(monkeypatch):
    monkeypatch.setenv("TARS_ENV", "development")
    monkeypatch.setenv("TARS_BACKEND_RELOAD", "true")
    settings = Settings(_env_file=None)
    assert settings.tars_env == "development"
    assert settings.tars_backend_reload is True
