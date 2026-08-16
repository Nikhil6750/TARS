"""Pure helpers used by black-box security acceptance checks."""

from __future__ import annotations

from typing import Any


FORBIDDEN_EXECUTION_TERMS = (
    "execute",
    "execution",
    "broker",
    "order",
    "position/close",
)
MUTATING_METHODS = {"post", "put", "patch", "delete"}


def find_live_execution_operations(openapi: dict[str, Any]) -> list[str]:
    """Return mutating operations whose paths resemble trade execution."""

    paths = openapi.get("paths", {})
    if not isinstance(paths, dict):
        return ["<invalid OpenAPI paths object>"]
    return sorted(
        f"{method.upper()} {path}"
        for path, operations in paths.items()
        if isinstance(path, str) and isinstance(operations, dict)
        for method in operations
        if method.casefold() in MUTATING_METHODS
        and any(term in path.casefold() for term in FORBIDDEN_EXECUTION_TERMS)
    )
