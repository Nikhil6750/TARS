from __future__ import annotations

import importlib
from collections.abc import Iterable, Mapping

from app.action_contracts import Skill


class SkillRegistryError(ValueError):
    pass


class SkillRegistry:
    """Runtime-owned registry. Skills cannot dispatch or grant themselves permission."""

    def __init__(self, skills: Iterable[Skill] = ()) -> None:
        self._skills: dict[str, Skill] = {}
        for skill in skills:
            self.register(skill)

    def register(self, skill: Skill) -> None:
        if not isinstance(skill, Skill):
            raise SkillRegistryError("Registered object does not satisfy the Skill protocol")
        name = getattr(skill, "name", "")
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise SkillRegistryError("Skill name must be a non-empty, trimmed string")
        if name in self._skills:
            raise SkillRegistryError(f"Skill {name!r} is already registered")
        capabilities = getattr(skill, "capabilities", ())
        if not isinstance(capabilities, tuple) or not all(
            isinstance(item, str) and item for item in capabilities
        ):
            raise SkillRegistryError(f"Skill {name!r} has invalid capabilities")
        self._skills[name] = skill

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def require(self, name: str) -> Skill:
        skill = self.get(name)
        if skill is None:
            raise SkillRegistryError(f"Unknown skill: {name}")
        return skill

    def describe(self) -> list[dict[str, object]]:
        return [
            {"name": name, "capabilities": list(skill.capabilities)}
            for name, skill in sorted(self._skills.items())
        ]


def build_skill_registry() -> SkillRegistry:
    """Load Claude's skill package when present, while keeping this branch bootable alone.

    The integration contract permits either a ``SKILLS`` mapping/iterable or a
    ``get_skills()`` factory. Invalid exported registries fail closed at startup.
    """

    registry = SkillRegistry()
    try:
        module = importlib.import_module("skills")
    except ModuleNotFoundError as exc:
        if exc.name == "skills":
            return registry
        raise

    exported = getattr(module, "SKILLS", None)
    if exported is None:
        factory = getattr(module, "get_skills", None)
        if factory is None:
            return registry
        exported = factory()

    values = exported.values() if isinstance(exported, Mapping) else exported
    for skill in values:
        registry.register(skill)
    return registry
