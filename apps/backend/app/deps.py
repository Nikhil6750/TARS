"""FastAPI dependency accessors — thin wrappers pulling shared singletons
off `request.app.state`, set up once in `app.main`'s lifespan."""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

from app.config import Settings, get_settings
from app.db import Database
from app.event_bus import EventBus
from app.latency_store import LatencyTraceStore
from app.ws_manager import ConnectionManager
from events.service import EventService

if TYPE_CHECKING:
    from actions.frontend_bridge import FrontendCommandBridge
    from actions.plan_runtime import PlanRuntime
    from actions.runtime import ActionRuntime
    from agent_runtime.runtime import AgentRuntime as AgentJobRuntime
    from agents.base import Agent, AgentRuntime
    from app.voice_state import VoiceProviders
    from assistant.chart_analysis import ChartAnalysisService
    from assistant.provider import AssistantProvider
    from assistant.router import AssistantRouter
    from memory.service import MemoryService
    from orchestrator.orchestrator import TarsOrchestrator
    from trading.context import TradingContextBuilder
    from trading.provider import StrategyProvider


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_event_service(request: Request) -> EventService:
    return EventService(request.app.state.db.conn)


def get_ws_manager(request: Request) -> ConnectionManager:
    return request.app.state.ws_manager


def get_event_bus(request: Request) -> EventBus:
    return request.app.state.event_bus


def get_settings_dep() -> Settings:
    return get_settings()


def get_assistant_provider(request: Request) -> AssistantProvider:
    return request.app.state.assistant_provider


def get_assistant_router(request: Request) -> AssistantRouter:
    from assistant.conversation_store import ConversationStore
    from assistant.router import AssistantRouter

    db: Database = request.app.state.db
    return AssistantRouter(
        event_service=EventService(db.conn),
        conversation_store=ConversationStore(db.conn),
        provider=request.app.state.assistant_provider,
        memory_service=request.app.state.memory_service,
        trace_store=getattr(request.app.state, "latency_trace_store", None),
    )


def get_memory_service(request: Request) -> MemoryService:
    return request.app.state.memory_service


def get_voice_providers(request: Request) -> VoiceProviders:
    return request.app.state.voice_providers


def get_action_runtime(request: Request) -> ActionRuntime:
    return request.app.state.action_runtime


def get_plan_runtime(request: Request) -> PlanRuntime:
    return request.app.state.plan_runtime


def get_frontend_bridge(request: Request) -> FrontendCommandBridge:
    return request.app.state.frontend_bridge


def get_chart_analysis_service(request: Request) -> ChartAnalysisService:
    return request.app.state.chart_analysis_service


def get_latency_trace_store(request: Request) -> LatencyTraceStore:
    return request.app.state.latency_trace_store


def get_strategy_provider(request: Request) -> StrategyProvider:
    return request.app.state.strategy_provider


def get_trading_context_builder(request: Request) -> TradingContextBuilder:
    return request.app.state.trading_context_builder


def get_agent_runtime(request: Request) -> AgentRuntime:
    """The TARS core stream's bounded ON_DEMAND/SCHEDULED/CONTINUOUS worker
    framework (agents/base.py) -- see get_agent_job_runtime for the
    separate, LLM-decision-loop job runtime from the agent-runtime stream."""
    return request.app.state.agent_runtime


def get_agents(request: Request) -> dict[str, Agent]:
    return request.app.state.agents


def get_agent_job_runtime(request: Request) -> AgentJobRuntime:
    """The agent-runtime stream's durable, provider-neutral LLM-decision-loop
    job runtime (agent_runtime/runtime.py) -- distinct from get_agent_runtime
    above. Renamed at integration time to resolve the naming collision
    between the two independently-built "agents" packages."""
    return request.app.state.agent_job_runtime


def get_orchestrator(request: Request) -> TarsOrchestrator:
    from assistant.conversation_store import ConversationStore
    from orchestrator.orchestrator import TarsOrchestrator

    db: Database = request.app.state.db
    return TarsOrchestrator(
        assistant_router=get_assistant_router(request),
        action_runtime=request.app.state.action_runtime,
        memory_service=request.app.state.memory_service,
        conversation_store=ConversationStore(db.conn),
        agent_runtime=getattr(request.app.state, "agent_runtime", None),
        agents=getattr(request.app.state, "agents", None),
        skill_manager=getattr(request.app.state, "skill_manager", None),
        pending_skill_confirmations=getattr(request.app.state, "pending_skill_confirmations", None),
    )
