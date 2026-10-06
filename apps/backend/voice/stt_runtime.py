"""Local-STT runtime support: model cache location, execution-backend
detection, health state, transcript normalisation and a cheap connectivity probe.

Nothing here talks to a cloud service except `ConnectivityMonitor`, which only
opens a TCP socket (no audio, no payload) to decide whether online reasoning is
reachable.
"""
from __future__ import annotations

import os
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

ALLOWED_MODELS = ("base.en", "small.en", "medium.en")
DEFAULT_MODEL = "small.en"
STT_MODES = ("faster_whisper", "gemini_live", "auto")


class STTState(str, Enum):
    LOADING = "STT_LOADING"
    READY = "STT_READY"
    ERROR = "STT_ERROR"


def resolve_model_dir(configured: str | None = None) -> Path:
    """Model cache lives outside the repo: %LOCALAPPDATA%\\TARS\\models\\whisper."""
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured)))
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "TARS" / "models" / "whisper"


def resolve_model_name(name: str | None) -> str:
    """Only the vetted English models may be selected by config; a typo must not
    trigger a surprise multi-GB download of some other checkpoint."""
    value = (name or DEFAULT_MODEL).strip()
    return value if value in ALLOWED_MODELS or Path(value).is_dir() else DEFAULT_MODEL


def register_cuda_dll_dirs() -> list[str]:
    """pip-installed `nvidia-*-cu12` wheels keep their DLLs under site-packages/nvidia/*/bin, which
    Windows does not search by default. Returns the directories made loadable."""
    added: list[str] = []
    if not hasattr(os, "add_dll_directory"):
        return added
    import site
    import sys as _sys

    roots = [*site.getsitepackages(), site.getusersitepackages(), *_sys.path]
    for root in dict.fromkeys(roots):
        nvidia = Path(root) / "nvidia"
        if nvidia.is_dir():
            for bin_dir in nvidia.glob("*/bin"):
                try:
                    os.add_dll_directory(str(bin_dir))
                    os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
                    added.append(str(bin_dir))
                except OSError:
                    pass
    return added


def detect_backend(requested: str = "auto") -> tuple[str, str]:
    """Returns (device, compute_type). GPU only when CTranslate2 can actually see a CUDA device."""
    requested = (requested or "auto").lower()
    if requested != "cpu":
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() > 0:
                types = ctranslate2.get_supported_compute_types("cuda")
                for preferred in ("int8_float16", "float16"):
                    if preferred in types:
                        return "cuda", preferred
        except Exception:
            pass
    return "cpu", "int8"


@dataclass
class STTHealth:
    provider: str = "faster_whisper"
    model: str = DEFAULT_MODEL
    backend: str = "unknown"
    compute_type: str = ""
    state: STTState = STTState.LOADING
    offline_capable: bool = True
    detail: str | None = None
    fallback: str | None = None
    model_dir: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict:
        return {
            "provider": self.provider, "model": self.model, "backend": self.backend,
            "compute_type": self.compute_type, "state": self.state.value,
            "offline_capable": self.offline_capable, "detail": self.detail,
            "fallback": self.fallback,
        }


# ---- connectivity -----------------------------------------------------------------

class ConnectivityMonitor:
    """Cached reachability check; `force_offline` is the application-layer kill switch
    used for offline acceptance (no OS network changes)."""

    def __init__(self, *, force_offline: bool = False, ttl: float = 10.0,
                 probe=None, hosts=(("1.1.1.1", 443), ("8.8.8.8", 53))):
        self.force_offline, self.ttl, self.hosts = force_offline, ttl, hosts
        self._probe = probe or self._tcp_probe
        self._cached: tuple[float, bool] | None = None

    def _tcp_probe(self) -> bool:
        for host, port in self.hosts:
            try:
                with socket.create_connection((host, port), timeout=1.2):
                    return True
            except OSError:
                continue
        return False

    def online(self) -> bool:
        if self.force_offline or os.environ.get("TARS_FORCE_OFFLINE", "").lower() in {"1", "true", "yes"}:
            return False
        now = time.monotonic()
        if self._cached and now - self._cached[0] < self.ttl:
            return self._cached[1]
        value = bool(self._probe())
        self._cached = (now, value)
        return value


OFFLINE_REASONING_MESSAGE = (
    "I can hear you and control local functions, but market and news reasoning "
    "currently needs an internet connection."
)
OFFLINE_GENERIC_MESSAGE = (
    "I understood what you said, but this request needs online reasoning."
)


# ---- transcript normalisation ------------------------------------------------------

_SPACED_TICKER = re.compile(r"\b(?:[A-Za-z]\s){5}[A-Za-z]\b")
_KNOWN_TICKERS = {"XAUUSD", "EURUSD", "GBPUSD", "BTCUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF",
                  "NZDUSD", "XAGUSD", "ETHUSD"}
_SPOKEN_FIXES = (
    (re.compile(r"\btrading\s+view\b", re.I), "TradingView"),
    (re.compile(r"\bpower\s+shell\b", re.I), "PowerShell"),
    (re.compile(r"\bgold\s+usd\b", re.I), "XAUUSD"),
)


def normalize_stt_text(text: str) -> str:
    """Conservative: only spelled-out *known* tickers and two-word product names are merged.
    Never invents a command the user did not say."""
    out = text.strip()

    def merge(match: re.Match) -> str:
        joined = re.sub(r"\s", "", match.group(0)).upper()
        return joined if joined in _KNOWN_TICKERS else match.group(0)

    out = _SPACED_TICKER.sub(merge, out)
    # "XAU USD", "euro USD" style splits of a known six-letter ticker
    out = re.sub(r"\b([A-Za-z]{3})\s+(USD)\b",
                 lambda m: (m.group(1) + m.group(2)).upper()
                 if (m.group(1) + m.group(2)).upper() in _KNOWN_TICKERS else m.group(0), out)
    for pattern, replacement in _SPOKEN_FIXES:
        out = pattern.sub(replacement, out)
    return out
