"""Agent framework API — inspect configured agents and trigger an ON_DEMAND
run of any of them (regardless of their *default* configured mode: a
CONTINUOUS agent like `setup_watch_agent` can still be nudged to run one
extra bounded iteration right now, same as `AgentRuntime.run_on_demand`
allows). Scheduling/continuous lifecycle management is process-internal
(wired at startup in `app/main.py`'s lifespan) -- this router is read/trigger
only, matching the rest of this backend's "backend decides, never a bare
passthrough" posture.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from agents.base import Agent, AgentRuntime
from app.deps import get_agent_runtime, get_agents

router = APIRouter(tags=["agents"])


@router.get("/api/v1/agents")
async def list_agents(agents: dict[str, Agent] = Depends(get_agents)) -> list[dict]:
    return [
        {
            "name": agent.name,
            "mode": agent.config.mode.value,
            "interval_seconds": agent.config.interval_seconds,
            "timeout_seconds": agent.config.timeout_seconds,
            "max_iterations": agent.config.max_iterations,
        }
        for agent in agents.values()
    ]


@router.post("/api/v1/agents/{name}/run")
async def run_agent(
    name: str,
    agents: dict[str, Agent] = Depends(get_agents),
    agent_runtime: AgentRuntime = Depends(get_agent_runtime),
) -> dict:
    agent = agents.get(name)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {name}")
    result = await agent_runtime.run_on_demand(agent, trigger="api")
    return {
        "status": result.status.value,
        "summary": result.summary,
        "error": result.error,
        "data": result.data,
    }


@router.get("/api/v1/agents/runs")
async def list_agent_runs(
    name: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    agent_runtime: AgentRuntime = Depends(get_agent_runtime),
) -> list[dict]:
    return await agent_runtime.get_recent_runs(name, limit)
