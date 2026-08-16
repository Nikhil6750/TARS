"""Local wake-word detection via openWakeWord — WakeWordProvider per
ADR-006/011. The target phrase is "TARS", which has no pretrained
openWakeWord model (the library ships generic phrases like "hey jarvis",
not custom ones) — detecting it requires a custom-trained model the
openWakeWord training pipeline can produce, which is outside this phase's
scope (see AGENTS.md: "If custom TARS wake detection requires model
training not available in this phase, implement the correct provider
boundary ... while clearly recording the custom model requirement").

This adapter is fully wired to run any openWakeWord-compatible ONNX/tflite
model at `WAKE_WORD_MODEL_PATH`; without one configured it refuses clearly
rather than silently detecting nothing or detecting the wrong phrase.
Push-to-talk/keyboard activation (voice/manual_activation.py) is the
guaranteed fallback regardless of this provider's state, per ADR-006.
"""
from __future__ import annotations

import numpy as np

from voice.errors import VoiceProviderError
from voice.interfaces import WakeWordProvider, WakeWordResult


class OpenWakeWordProvider(WakeWordProvider):
    name = "openwakeword"
    sample_rate = 16000

    def __init__(self, model_path: str, phrase: str = "TARS", threshold: float = 0.5):
        if not model_path:
            raise VoiceProviderError(
                "openWakeWord selected but WAKE_WORD_MODEL_PATH is not set — "
                "no pretrained openWakeWord model exists for the phrase "
                "'TARS'; a custom model must be trained with openWakeWord's "
                "training pipeline and its path configured here. Until then, "
                "use push-to-talk/keyboard activation (always available)."
            )
        try:
            from openwakeword.model import Model
        except ImportError as exc:
            raise VoiceProviderError(
                "openwakeword is not installed — pip install -r requirements-voice.txt"
            ) from exc

        self._phrase = phrase
        self._threshold = threshold
        try:
            self._model = Model(wakeword_models=[model_path], inference_framework="onnx")
        except Exception as exc:
            raise VoiceProviderError(
                f"failed to load openWakeWord model at '{model_path}': {exc}"
            ) from exc

    async def process_chunk(self, pcm_audio: bytes) -> WakeWordResult:
        samples = np.frombuffer(pcm_audio, dtype=np.int16)
        try:
            predictions = self._model.predict(samples)
        except Exception as exc:
            raise VoiceProviderError(f"openWakeWord inference failed: {exc}") from exc

        best_model, best_score = max(predictions.items(), key=lambda kv: kv[1], default=(None, 0.0))
        if best_model is not None and best_score >= self._threshold:
            return WakeWordResult(detected=True, phrase=self._phrase, score=float(best_score))
        return WakeWordResult(detected=False, score=float(best_score) if best_model else None)
