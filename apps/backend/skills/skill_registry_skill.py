"""`skills` skill -- the ActionRuntime surface for skill_registry.SkillManager
(search/inspect/install/uninstall/update/list/use). Every state-changing
action (install/uninstall/update) is CONFIRM_REQUIRED, both declared here
and re-derived by PermissionEngine (actions/permissions.py) independently
-- installing a skill never bypasses the same confirmation flow any other
state-changing action goes through. `use_skill` only ever reads an
already-installed SKILL.md's text; it never executes anything itself --
real actions the skill's instructions describe still have to go through
their own separate ActionRuntime calls (terminal/filesystem/browser/...),
each re-classified and re-confirmed independently. A SKILL.md's own text
has no path to change that -- this skill never interprets or acts on
instructions found inside a bundle, it only returns them as data.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.action_contracts import (
    ActionRequest,
    ActionResult,
    ActionStatus,
    BaseSkill,
    RiskLevel,
    SkillExecutionError,
    SkillValidationError,
)
from skill_registry.manager import SkillManager

_READ_ONLY_ACTIONS = {"search_skills", "inspect_skill", "list_installed", "use_skill"}
_CONFIRM_REQUIRED_ACTIONS = {"install_skill", "uninstall_skill", "update_skill"}


class SkillRegistrySkill(BaseSkill):
    name = "skills"
    description = (
        "Search the local Hermes skill catalog, install/update/uninstall skill "
        "bundles into the TARS Obsidian vault, and load an installed skill's "
        "instructions for the current task."
    )
    capabilities: tuple[str, ...] = (
        "search_skills",
        "inspect_skill",
        "list_installed",
        "install_skill",
        "uninstall_skill",
        "update_skill",
        "use_skill",
    )

    def __init__(self, manager: SkillManager | None = None) -> None:
        self._manager = manager

    async def health(self) -> dict[str, Any]:
        return {"available": self._manager is not None}

    def classify_risk(self, action: str, arguments: dict[str, Any]) -> RiskLevel:
        if action in _CONFIRM_REQUIRED_ACTIONS:
            return RiskLevel.CONFIRM_REQUIRED
        if action in _READ_ONLY_ACTIONS:
            return RiskLevel.READ_ONLY
        return RiskLevel.BLOCKED

    async def validate(self, action: str, arguments: dict[str, Any]) -> None:
        if action == "search_skills":
            if not isinstance(arguments.get("query"), str) or not arguments["query"].strip():
                raise SkillValidationError("search_skills requires non-empty 'query'")
        elif action in ("inspect_skill", "install_skill", "uninstall_skill", "update_skill"):
            if not isinstance(arguments.get("identifier"), str) or not arguments["identifier"].strip():
                raise SkillValidationError(f"{action} requires non-empty 'identifier'")
        elif action == "list_installed":
            pass
        elif action == "use_skill":
            if not isinstance(arguments.get("identifier"), str) or not arguments["identifier"].strip():
                raise SkillValidationError("use_skill requires non-empty 'identifier'")
        else:
            raise SkillValidationError(f"unsupported skills action '{action}'")

    async def execute(self, request: ActionRequest) -> ActionResult:
        if self._manager is None:
            raise SkillExecutionError("skill registry is not wired in (no SkillManager)")
        started = datetime.now(UTC)
        action = request.action
        args = request.arguments

        if action == "search_skills":
            results = await self._manager.search_skills(args["query"], limit=args.get("limit", 10))
            return self._result(
                request, ActionStatus.SUCCEEDED,
                f"Found {len(results)} skill(s) for '{args['query']}'.",
                risk_level=RiskLevel.READ_ONLY, data={"results": results}, started_at=started,
            )

        if action == "inspect_skill":
            record = await self._manager.inspect_skill(args["identifier"])
            if record is None:
                return self._result(
                    request, ActionStatus.FAILED, "Skill not found in catalog.",
                    risk_level=RiskLevel.READ_ONLY, error=f"identifier not found: {args['identifier']}", started_at=started,
                )
            return self._result(
                request, ActionStatus.SUCCEEDED, f"Inspected {args['identifier']}.",
                risk_level=RiskLevel.READ_ONLY, data={"skill": record}, started_at=started,
            )

        if action == "list_installed":
            installed = await self._manager.list_installed()
            return self._result(
                request, ActionStatus.SUCCEEDED, f"{len(installed)} skill(s) installed.",
                risk_level=RiskLevel.READ_ONLY, data={"installed": installed}, started_at=started,
            )

        if action == "install_skill":
            result = await self._manager.install_skill(args["identifier"])
            if not result.installed:
                return self._result(
                    request, ActionStatus.FAILED, "Skill install failed.",
                    risk_level=RiskLevel.CONFIRM_REQUIRED,
                    error="; ".join(result.findings) or "install failed",
                    started_at=started,
                )
            return self._result(
                request, ActionStatus.SUCCEEDED, f"Installed {args['identifier']}.",
                risk_level=RiskLevel.CONFIRM_REQUIRED,
                data={"local_path": result.local_path, "content_hash": result.content_hash},
                started_at=started,
            )

        if action == "uninstall_skill":
            ok = await self._manager.uninstall_skill(args["identifier"])
            if not ok:
                return self._result(
                    request, ActionStatus.FAILED, "Skill was not installed.",
                    risk_level=RiskLevel.CONFIRM_REQUIRED,
                    error=f"not currently installed: {args['identifier']}", started_at=started,
                )
            return self._result(
                request, ActionStatus.SUCCEEDED, f"Uninstalled {args['identifier']}.",
                risk_level=RiskLevel.CONFIRM_REQUIRED, data={}, started_at=started,
            )

        if action == "update_skill":
            result = await self._manager.update_skill(args["identifier"])
            if not result.installed:
                return self._result(
                    request, ActionStatus.FAILED, "Skill update failed.",
                    risk_level=RiskLevel.CONFIRM_REQUIRED,
                    error="; ".join(result.findings) or "update failed", started_at=started,
                )
            return self._result(
                request, ActionStatus.SUCCEEDED, f"Updated {args['identifier']}.",
                risk_level=RiskLevel.CONFIRM_REQUIRED,
                data={"local_path": result.local_path, "content_hash": result.content_hash},
                started_at=started,
            )

        if action == "use_skill":
            content = await self._manager.load_installed_content(args["identifier"])
            if content is None:
                return self._result(
                    request, ActionStatus.FAILED, "Skill is not installed.",
                    risk_level=RiskLevel.READ_ONLY,
                    error=f"not installed: {args['identifier']}", started_at=started,
                )
            await self._manager.record_invocation(
                args["identifier"], task=args.get("task", ""), result_status="loaded"
            )
            return self._result(
                request, ActionStatus.SUCCEEDED, f"Loaded {args['identifier']}.",
                risk_level=RiskLevel.READ_ONLY, data={"content": content}, started_at=started,
            )

        raise SkillExecutionError(f"unsupported skills action '{action}'")
