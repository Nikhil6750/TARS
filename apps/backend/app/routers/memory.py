from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.deps import get_memory_service
from memory.service import MemoryService

router = APIRouter(prefix="/api/v1/memory", tags=["memory"])


@router.get("/search")
async def search(
    q: str = Query(min_length=1),
    limit: int = Query(default=10, ge=1, le=100),
    source: str | None = Query(default=None, pattern="^(conversation|vault)$"),
    memory: MemoryService = Depends(get_memory_service),
) -> list[dict]:
    return await memory.search(query=q, limit=limit, source=source)


@router.post("/reindex-vault")
async def reindex_vault(memory: MemoryService = Depends(get_memory_service)) -> dict:
    result = await memory.reindex_vault()
    return {
        "vault_path": result.vault_path,
        "indexed": result.indexed,
        "unchanged": result.unchanged,
        "removed": result.removed,
        "vault_missing": result.vault_missing,
    }
