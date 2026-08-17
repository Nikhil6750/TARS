"""`filesystem` skill -- read-only listing/search plus "open with the OS
default handler". Deliberately does NOT implement write/move/delete in this
milestone (see docs/coordination/wave2/M2A_SPEC.md): any action shaped like
a write/delete is rejected by `validate()` and classified `BLOCKED`, never
silently accepted and silently ignored.

Every path argument is boundary-checked against a small allowlist of safe
roots (the caller's home directory tree) *after* resolving `..`/symlinks --
`Path.resolve()` first, then `Path.relative_to()` against each allowed
root, so a traversal like `~/../../Windows/System32` cannot escape.
"""
from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path
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

_READ_ONLY_ACTIONS = {"list", "search"}
_ALLOWED_ACTIONS = _READ_ONLY_ACTIONS | {"open"}

_MAX_SEARCH_RESULTS = 200
_SEARCH_TIME_BUDGET_SECONDS = 5.0


def _safe_roots() -> list[Path]:
    # A small, explicit allowlist -- just the user's home directory tree.
    # Re-evaluated per call (not module-level) so tests can monkeypatch
    # Path.home() via HOME/USERPROFILE env vars without import-order games.
    return [Path.home().resolve()]


def resolve_within_safe_roots(raw_path: str) -> Path:
    """Resolves `raw_path` and verifies it falls under one of the allowed
    roots. Raises SkillValidationError otherwise. Accepts relative paths
    (resolved against the first safe root) or absolute paths (must already
    be inside an allowed root)."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise SkillValidationError("path must be a non-empty string")

    candidate = Path(raw_path).expanduser()
    roots = _safe_roots()
    if not candidate.is_absolute():
        candidate = roots[0] / candidate

    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise SkillValidationError(f"invalid path '{raw_path}': {exc}") from exc

    for root in roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue

    raise SkillValidationError(
        f"path '{raw_path}' resolves outside the allowed roots ({', '.join(str(r) for r in roots)})"
    )


class FilesystemSkill(BaseSkill):
    name = "filesystem"
    capabilities: tuple[str, ...] = ("list", "search", "open")

    def classify_risk(self, action: str, arguments: dict[str, Any]) -> RiskLevel:
        if action in _READ_ONLY_ACTIONS:
            return RiskLevel.READ_ONLY
        if action == "open":
            return RiskLevel.LOW_RISK
        # Anything write/delete/move-shaped (and anything else unknown) is
        # not implemented this milestone -- block rather than silently
        # no-op, per M2A_SPEC.md.
        return RiskLevel.BLOCKED

    async def validate(self, action: str, arguments: dict[str, Any]) -> None:
        if action not in _ALLOWED_ACTIONS:
            raise SkillValidationError(
                f"filesystem action '{action}' is not implemented in this milestone "
                "(read + open only -- no write/delete/move)"
            )

        path = arguments.get("path")
        resolved = resolve_within_safe_roots(path if isinstance(path, str) else "")

        if action == "search":
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                raise SkillValidationError("search requires non-empty 'query'")

        if action in ("list", "open") and not resolved.exists():
            raise SkillValidationError(f"path does not exist: '{path}'")

    async def execute(self, request: ActionRequest) -> ActionResult:
        started = datetime.now(UTC)
        if request.action == "list":
            return self._execute_list(request, started)
        if request.action == "search":
            return self._execute_search(request, started)
        if request.action == "open":
            return self._execute_open(request, started)
        raise SkillExecutionError(f"unsupported filesystem action '{request.action}'")

    def _execute_list(self, request: ActionRequest, started: datetime) -> ActionResult:
        resolved = resolve_within_safe_roots(request.arguments["path"])
        if not resolved.is_dir():
            return self._result(
                request,
                ActionStatus.FAILED,
                f"Not a directory: {resolved}",
                risk_level=RiskLevel.READ_ONLY,
                error=f"'{resolved}' is not a directory",
                started_at=started,
            )
        entries: list[dict[str, Any]] = []
        with os.scandir(resolved) as it:
            for entry in it:
                entries.append(
                    {
                        "name": entry.name,
                        "is_dir": entry.is_dir(follow_symlinks=False),
                    }
                )
        entries.sort(key=lambda e: (not e["is_dir"], str(e["name"]).lower()))
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            f"Listed {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} in {resolved}.",
            risk_level=RiskLevel.READ_ONLY,
            data={"path": str(resolved), "entries": entries},
            started_at=started,
        )

    def _execute_search(self, request: ActionRequest, started: datetime) -> ActionResult:
        resolved = resolve_within_safe_roots(request.arguments["path"])
        query = request.arguments["query"].strip().lower()
        if not resolved.is_dir():
            return self._result(
                request,
                ActionStatus.FAILED,
                f"Not a directory: {resolved}",
                risk_level=RiskLevel.READ_ONLY,
                error=f"'{resolved}' is not a directory",
                started_at=started,
            )

        # `path` defaults to the home directory (the only allowed safe root)
        # when the caller doesn't name a subfolder -- e.g. a deterministic
        # "search files for X" HUD/voice command with no location. Without a
        # time budget, `rglob("*")` over an entire real home directory (node_modules,
        # AppData, browser caches, etc.) can run for minutes with no way to
        # short-circuit before `_MAX_SEARCH_RESULTS` is reached, making the
        # action appear hung. Bounding by wall-clock time, not just match
        # count, keeps this a read-only op that always returns promptly.
        matches: list[str] = []
        truncated = False
        deadline = time.monotonic() + _SEARCH_TIME_BUDGET_SECONDS
        for path in resolved.rglob("*"):
            if query in path.name.lower():
                matches.append(str(path))
                if len(matches) >= _MAX_SEARCH_RESULTS:
                    truncated = True
                    break
            if time.monotonic() >= deadline:
                truncated = True
                break

        summary = f"Found {len(matches)} match(es) for '{request.arguments['query']}' under {resolved}."
        if truncated:
            summary += " (search stopped early -- narrow 'path' for a complete result)"

        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            summary,
            risk_level=RiskLevel.READ_ONLY,
            data={
                "path": str(resolved),
                "query": request.arguments["query"],
                "matches": matches,
                "truncated": truncated,
            },
            started_at=started,
        )

    def _execute_open(self, request: ActionRequest, started: datetime) -> ActionResult:
        resolved = resolve_within_safe_roots(request.arguments["path"])
        if not resolved.exists():
            return self._result(
                request,
                ActionStatus.FAILED,
                f"Path does not exist: {resolved}",
                risk_level=RiskLevel.LOW_RISK,
                error=f"'{resolved}' does not exist",
                started_at=started,
            )
        try:
            os.startfile(str(resolved))  # type: ignore[attr-defined]  # Windows-only
        except OSError as exc:
            raise SkillExecutionError(f"failed to open '{resolved}': {exc}") from exc

        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            f"Opened {resolved} with the default handler.",
            risk_level=RiskLevel.LOW_RISK,
            data={"path": str(resolved)},
            started_at=started,
        )
