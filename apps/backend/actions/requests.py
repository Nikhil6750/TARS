from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from actions.errors import AssistantActionError
from app.action_contracts import (
    ActionRequest,
    ActionSource,
    ActiveWindowContext,
)
from app.contracts import validate_action_request

_ALLOWED_PROPOSAL_FIELDS = {"skill", "action", "arguments"}
_ALLOWED_CONTEXT_FIELDS = {
    "executable",
    "process_id",
    "window_title",
    "window_bounds",
    "captured_at",
}
_ALLOWED_BOUNDS_FIELDS = {"x", "y", "width", "height"}
_FOCUS = re.compile(r"^\s*focus\s+(?:the\s+)?(?P<target>[\w .()-]+?)\s*$", re.I)
_LAUNCH = re.compile(r"^\s*(?:launch|start)\s+(?:the\s+)?(?P<target>[\w .()\\:-]+?)\s*$", re.I)
_OPEN_URL = re.compile(r"^\s*open\s+(?P<url>https?://\S+)\s*$", re.I)


class ActionRequestFactory:
    """Convert untrusted structured assistant output into a canonical request.

    The assistant may propose only skill/action/arguments. Identity, origin,
    timestamp and active-window context are attached by trusted application code.
    Permission and confirmation fields are not accepted at all.
    """

    @staticmethod
    def from_assistant(
        proposal: Any,
        *,
        source: ActionSource,
        active_context: ActiveWindowContext | dict[str, Any] | None = None,
    ) -> ActionRequest:
        if not isinstance(proposal, dict):
            raise AssistantActionError("Assistant action proposal must be an object")
        unknown = set(proposal) - _ALLOWED_PROPOSAL_FIELDS
        missing = {"skill", "action"} - set(proposal)
        if unknown:
            raise AssistantActionError(
                f"Assistant action proposal has forbidden fields: {', '.join(sorted(unknown))}"
            )
        if missing:
            raise AssistantActionError(
                f"Assistant action proposal is missing: {', '.join(sorted(missing))}"
            )
        if not isinstance(proposal.get("skill"), str) or not proposal["skill"].strip():
            raise AssistantActionError("Assistant skill must be a non-empty string")
        if not isinstance(proposal.get("action"), str) or not proposal["action"].strip():
            raise AssistantActionError("Assistant action must be a non-empty string")
        arguments = proposal.get("arguments", {})
        if not isinstance(arguments, dict):
            raise AssistantActionError("Assistant action arguments must be an object")
        _validate_context_shape(active_context)
        try:
            context = (
                ActiveWindowContext.model_validate(active_context)
                if active_context is not None
                else None
            )
            request = ActionRequest(
                skill=proposal["skill"],
                action=proposal["action"],
                arguments=arguments,
                source=source,
                active_context=context,
            )
        except ValidationError as exc:
            raise AssistantActionError(str(exc)) from exc
        validate_action_request(request.to_contract_dict())
        return request


class DeterministicActionRouter:
    """Small fixed-phrase router; unrecognized language remains with the assistant."""

    def resolve(
        self,
        text: str,
        *,
        active_context: ActiveWindowContext | dict[str, Any] | None = None,
    ) -> ActionRequest | None:
        match = _FOCUS.fullmatch(text)
        if match:
            return ActionRequestFactory.from_assistant(
                {
                    "skill": "windows_app",
                    "action": "focus",
                    "arguments": {"target": match.group("target")},
                },
                source=ActionSource.deterministic,
                active_context=active_context,
            )
        match = _LAUNCH.fullmatch(text)
        if match:
            return ActionRequestFactory.from_assistant(
                {
                    "skill": "windows_app",
                    "action": "launch",
                    "arguments": {"target": match.group("target")},
                },
                source=ActionSource.deterministic,
                active_context=active_context,
            )
        match = _OPEN_URL.fullmatch(text)
        if match:
            return ActionRequestFactory.from_assistant(
                {
                    "skill": "browser",
                    "action": "open_url",
                    "arguments": {"url": match.group("url")},
                },
                source=ActionSource.deterministic,
                active_context=active_context,
            )
        return None


def _validate_context_shape(active_context: Any) -> None:
    if active_context is None or isinstance(active_context, ActiveWindowContext):
        return
    if not isinstance(active_context, dict):
        raise AssistantActionError("Active context must be an object or null")
    unknown = set(active_context) - _ALLOWED_CONTEXT_FIELDS
    if unknown:
        raise AssistantActionError(
            f"Active context has forbidden fields: {', '.join(sorted(unknown))}"
        )
    bounds = active_context.get("window_bounds")
    if bounds is not None:
        if not isinstance(bounds, dict) or set(bounds) != _ALLOWED_BOUNDS_FIELDS:
            raise AssistantActionError(
                "window_bounds must contain x, y, width and height only"
            )
