from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError

from agent_runtime.contracts import AgentJob
from agent_runtime.errors import (
    AgentConflictError,
    AgentContractError,
    AgentJobNotFoundError,
    DuplicateJobError,
)
from agent_runtime.runtime import AgentRuntime
from app.deps import get_agent_job_runtime

# Distinct prefix from app/routers/agents.py's /api/v1/agents (the TARS
# core stream's bounded ON_DEMAND/SCHEDULED/CONTINUOUS worker framework) --
# this router fronts a different system: a durable, provider-neutral
# LLM-decision-loop job runtime (integration-time rename to resolve the
# naming collision between the two independently-built "agents" packages;
# see the merge commit for the reconciliation).
router = APIRouter(prefix="/api/v1/agent-runtime", tags=["agent-runtime"])


@router.post("")
async def submit_agent_job(
    request: Request,
    run_now: bool = Query(default=True),
    runtime: AgentRuntime = Depends(get_agent_job_runtime),
) -> dict[str, Any]:
    raw = await _json_object(request)
    try:
        run = await runtime.submit(AgentJob.model_validate(raw), run_now=run_now)
    except (ValidationError, AgentContractError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DuplicateJobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return run.model_dump(mode="json")


@router.get("/{job_id}")
async def get_agent_job(
    job_id: UUID, runtime: AgentRuntime = Depends(get_agent_job_runtime)
) -> dict[str, Any]:
    try:
        return (await runtime.store.get_run(job_id)).model_dump(mode="json")
    except AgentJobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{job_id}/audit")
async def get_agent_audit(
    job_id: UUID, runtime: AgentRuntime = Depends(get_agent_job_runtime)
) -> list[dict[str, Any]]:
    try:
        return await runtime.store.list_audit(job_id)
    except AgentJobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{job_id}/run")
async def run_agent_job(
    job_id: UUID, runtime: AgentRuntime = Depends(get_agent_job_runtime)
) -> dict[str, Any]:
    try:
        return (await runtime.run(job_id)).model_dump(mode="json")
    except AgentJobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AgentConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{job_id}/cancel")
async def cancel_agent_job(
    job_id: UUID, runtime: AgentRuntime = Depends(get_agent_job_runtime)
) -> dict[str, Any]:
    try:
        return (await runtime.cancel(job_id)).model_dump(mode="json")
    except AgentJobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{job_id}/recover")
async def recover_agent_job(
    job_id: UUID, runtime: AgentRuntime = Depends(get_agent_job_runtime)
) -> dict[str, Any]:
    try:
        return (await runtime.recover(job_id)).model_dump(mode="json")
    except AgentJobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AgentConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        raw: Any = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Invalid JSON payload") from exc
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="Payload must be a JSON object")
    return raw
