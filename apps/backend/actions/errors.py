from __future__ import annotations


class ActionRuntimeError(RuntimeError):
    """Base class for errors whose API representation is safe to expose."""


class DuplicateActionError(ActionRuntimeError):
    pass


class ActionNotFoundError(ActionRuntimeError):
    pass


class ConfirmationReplayError(ActionRuntimeError):
    pass


class InvalidConfirmationError(ActionRuntimeError):
    pass


class AssistantActionError(ValueError):
    pass
