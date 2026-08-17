from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from actions.plan_models import ActionPlan, ActionStep, PlanProvenance
from actions.plan_runtime import PlanValidationError

_PLAN_FIELDS = {"goal", "context", "steps"}
_STEP_FIELDS = {
    "step_id",
    "skill",
    "action",
    "arguments",
    "expected_result",
    "risk_level",
    "dependencies",
    "recovery",
}


class ActionPlanFactory:
    """Turn assistant JSON into plan data without granting it runtime authority."""

    @staticmethod
    def from_assistant(proposal: Any) -> ActionPlan:
        if not isinstance(proposal, dict) or set(proposal) - _PLAN_FIELDS:
            raise PlanValidationError("Assistant plan may contain goal, context and steps only")
        goal = proposal.get("goal")
        if not isinstance(goal, str):
            raise PlanValidationError("Assistant plan goal must be a string")
        if not isinstance(proposal.get("steps"), list):
            raise PlanValidationError("Assistant plan steps must be a list")
        steps: list[ActionStep] = []
        for raw in proposal["steps"]:
            if not isinstance(raw, dict) or set(raw) - _STEP_FIELDS:
                raise PlanValidationError("Assistant step contains forbidden control fields")
            try:
                steps.append(ActionStep.model_validate(raw))
            except ValidationError as exc:
                raise PlanValidationError(str(exc)) from exc
        try:
            return ActionPlan(
                goal=goal,
                context=proposal.get("context", {}),
                steps=steps,
                provenance=PlanProvenance.ASSISTANT,
            )
        except ValidationError as exc:
            raise PlanValidationError(str(exc)) from exc
