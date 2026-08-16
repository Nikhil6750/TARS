class AssistantProviderError(RuntimeError):
    """Raised when a configured AssistantProvider cannot produce a reply —
    missing binary, missing API key, network failure, non-zero exit, etc.
    Callers catch this and surface a graceful error, never a raw traceback."""
