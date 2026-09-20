"""`obsidian` skill -- read-only search/read over the vault, wrapping the
EXISTING `memory.service.MemoryService` (FTS5 + vault indexing). This module
does not reimplement indexing or search; `search` delegates directly to
`MemoryService.search(..., source="vault")` and preserves its
`source`/`source_id` provenance fields verbatim in `ActionResult.data`.

`read` is the one piece `MemoryService` does not already expose (it indexes
snippets, not full note bodies) -- it resolves a vault-relative path with
the same "resolve, then verify it's still under the vault root"
boundary-check pattern used by the `filesystem` skill, and reads the file
directly, mirroring how `memory/vault.py` itself reads vault files
(`read_text(encoding="utf-8", errors="replace")`).
"""
from __future__ import annotations

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
from memory.service import MemoryService

_ACTIONS = {"search", "read"}


class ObsidianSkill(BaseSkill):
    name = "obsidian"
    description = "Search and read notes in the indexed Obsidian research vault."
    capabilities: tuple[str, ...] = ("search", "read")

    def __init__(self, memory_service: MemoryService, vault_path: str):
        self._memory = memory_service
        self._vault_root = Path(vault_path)

    def classify_risk(self, action: str, arguments: dict[str, Any]) -> RiskLevel:
        if action in _ACTIONS:
            return RiskLevel.READ_ONLY
        return RiskLevel.BLOCKED

    async def validate(self, action: str, arguments: dict[str, Any]) -> None:
        if action == "search":
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                raise SkillValidationError("search requires non-empty 'query'")
        elif action == "read":
            path = arguments.get("path")
            if not isinstance(path, str) or not path.strip():
                raise SkillValidationError("read requires non-empty 'path'")
            self._resolve_note_path(path)
        else:
            raise SkillValidationError(f"unsupported obsidian action '{action}'")

    def _resolve_note_path(self, rel_path: str) -> Path:
        vault_root = self._vault_root.resolve()
        try:
            candidate = (vault_root / rel_path).resolve(strict=False)
        except OSError as exc:
            raise SkillValidationError(f"invalid vault path '{rel_path}': {exc}") from exc
        try:
            candidate.relative_to(vault_root)
        except ValueError as exc:
            raise SkillValidationError(f"path '{rel_path}' escapes the vault root") from exc
        return candidate

    async def execute(self, request: ActionRequest) -> ActionResult:
        started = datetime.now(UTC)
        if request.action == "search":
            return await self._execute_search(request, started)
        if request.action == "read":
            return self._execute_read(request, started)
        raise SkillExecutionError(f"unsupported obsidian action '{request.action}'")

    async def _execute_search(self, request: ActionRequest, started: datetime) -> ActionResult:
        query = request.arguments["query"]
        limit = request.arguments.get("limit", 10)
        if not isinstance(limit, int) or isinstance(limit, bool):
            limit = 10
        results = await self._memory.search(query, limit=limit, source="vault")
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            f"Found {len(results)} vault result(s) for '{query}'.",
            risk_level=RiskLevel.READ_ONLY,
            data={"results": results},
            started_at=started,
        )

    def _execute_read(self, request: ActionRequest, started: datetime) -> ActionResult:
        rel_path = request.arguments["path"]
        resolved = self._resolve_note_path(rel_path)
        if not resolved.is_file():
            return self._result(
                request,
                ActionStatus.FAILED,
                f"Note not found: {rel_path}",
                risk_level=RiskLevel.READ_ONLY,
                error=f"no such file in vault: '{rel_path}'",
                started_at=started,
            )
        content = resolved.read_text(encoding="utf-8", errors="replace")
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            f"Read note '{rel_path}' ({len(content)} chars).",
            risk_level=RiskLevel.READ_ONLY,
            data={"source": "vault", "source_id": rel_path, "content": content},
            started_at=started,
        )
