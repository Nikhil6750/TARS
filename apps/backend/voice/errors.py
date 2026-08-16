class VoiceProviderError(RuntimeError):
    """Raised when a configured voice provider (wake word, STT, TTS) cannot
    do its job — missing model file, missing package, decode failure. The
    voice pipeline catches this and degrades (e.g. falls through to
    push-to-talk) rather than crashing the session."""
