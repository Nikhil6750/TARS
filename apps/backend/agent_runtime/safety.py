from __future__ import annotations

from typing import Any

from actions.safety import redact_sensitive
from agent_runtime.errors import AgentContractError


def assert_secret_free(value: Any, *, label: str) -> None:
    """Reject secret-shaped input before durable agent storage."""

    if redact_sensitive(value) != value:
        raise AgentContractError(f"{label} contains secret-shaped data")


def audit_safe(value: Any) -> Any:
    return redact_sensitive(value)
