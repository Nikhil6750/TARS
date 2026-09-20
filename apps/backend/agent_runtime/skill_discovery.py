from __future__ import annotations

from actions.runtime import ActionRuntime
from agent_runtime.contracts import SkillDescriptor


class ActionRuntimeSkillDiscovery:
    """Read-only capability discovery from the authoritative action registry."""

    def __init__(self, action_runtime: ActionRuntime) -> None:
        self._action_runtime = action_runtime

    async def discover(self) -> tuple[SkillDescriptor, ...]:
        # SkillRegistry.describe() is async (it awaits each skill's
        # health() check -- see the TARS core stream's Unified Skill
        # system work) and its entries now also carry `description`/
        # `health`, which SkillDescriptor's StrictModel deliberately
        # doesn't declare (extra="forbid") -- pick out just the two fields
        # this contract actually needs rather than loosening its strictness.
        described = await self._action_runtime.registry.describe()
        return tuple(
            SkillDescriptor(name=item["name"], capabilities=tuple(item["capabilities"]))
            for item in described
        )
