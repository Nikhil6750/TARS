"""Resolves local Kokoro model/voices file paths, downloading them once if
missing. Uses the same cache directory and filenames as Pipecat's built-in
`KokoroTTSService` (`~/.cache/pipecat/kokoro-onnx`) so the realtime pipeline
and our standalone `KokoroTTSProvider` never download the model twice.
"""
from __future__ import annotations

import os
from pathlib import Path

from voice.errors import VoiceProviderError

CACHE_DIR = Path(os.path.expanduser("~/.cache/pipecat/kokoro-onnx"))
MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
VOICES_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"
)


def _download(url: str, dest: Path) -> None:
    import requests

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with requests.get(url, stream=True, timeout=300) as resp:
            resp.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
        tmp.replace(dest)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise VoiceProviderError(f"failed to download Kokoro model file from {url}: {exc}") from exc


def resolve_kokoro_paths(model_path: str | None, voices_path: str | None) -> tuple[str, str]:
    model_file = Path(model_path) if model_path else CACHE_DIR / "kokoro-v1.0.onnx"
    voices_file = Path(voices_path) if voices_path else CACHE_DIR / "voices-v1.0.bin"

    if not model_file.exists():
        _download(MODEL_URL, model_file)
    if not voices_file.exists():
        _download(VOICES_URL, voices_file)

    return str(model_file), str(voices_file)
