from __future__ import annotations

from actions.runtime import ActionRuntime
from agents.contracts import SkillDescriptor


class ActionRuntimeSkillDiscovery:
    """Read-only capability discovery from the authoritative action registry."""

    def __init__(self, action_runtime: ActionRuntime) -> None:
        self._action_runtime = action_runtime

    def discover(self) -> tuple[SkillDescriptor, ...]:
        return tuple(
            SkillDescriptor.model_validate(item)
            for item in self._action_runtime.registry.describe()
        )
