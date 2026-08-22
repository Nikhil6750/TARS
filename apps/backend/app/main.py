from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from actions.frontend_bridge import FrontendCommandBridge
from actions.permissions import PermissionEngine
from actions.plan_runtime import PlanRuntime
from actions.plan_store import PlanStore
from actions.registry import build_skill_registry
from actions.runtime import ActionRuntime
from actions.store import ActionStore
from agent_runtime.providers import IntelligenceProviderRegistry
from agent_runtime.quant_boundary import QuantBrainBoundary
from agent_runtime.runtime import AgentRuntime as AgentJobRuntime
from agent_runtime.store import AgentStore as AgentJobStore
from agents.base import AgentRuntime
from agents.chart_analysis_agent import ChartAnalysisAgent
from agents.models import AgentConfig, AgentMode
from agents.setup_watch_agent import SetupWatchAgent
from agents.store import AgentRunStore
from agents.trading_workspace_agent import TradingWorkspaceAgent
from app.body_limit import MaxBodySizeMiddleware
from app.config import get_settings
from app.db import build_database
from app.event_bus import EventBus
from app.observability import configure_tracing
from app.routers import (
    action_plans,
    actions,
    assistant,
    chart_watch,
    diagnostics,
    events,
    health,
    memory,
    runtime,
    voice,
    ws,
)
from app.routers import agent_runtime as agent_runtime_router
from app.routers import agents as agents_router
from app.scheduler import build_scheduler
from app.voice_state import VoiceProviders
from app.ws_manager import ConnectionManager
from assistant.chart_analysis import ChartAnalysisService
from assistant.factory import build_assistant_provider, build_chart_assistant_provider
from assistant.providers.mock import MockAssistantProvider
from events.generator import MockEventGenerator
from events.service import EventService
from memory.service import MemoryService
from trading.context import TradingContextBuilder
from trading.provider import build_strategy_provider

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tars.app")

# 16 MiB ceiling per security review, raised from the original 1 MiB to fit
# a base64-encoded full-monitor screenshot for POST /api/v1/assistant/
# analyze-chart (a 1920x1080 BMP capture is ~6 MiB raw, ~8 MiB base64) --
# still a bounded ceiling appropriate for this backend's localhost-only,
# single-user threat model (see the 0.0.0.0-binding warning below).
MAX_BODY_BYTES = 16 * 1_048_576


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_tracing("tars-backend", settings.otel_exporter_otlp_endpoint)

    from app.readiness import (
        NOT_CONFIGURED_MESSAGE,
        REQUIRED_ASSISTANT_PROVIDER,
        REQUIRED_STT_PROVIDER,
        REQUIRED_TTS_PROVIDER,
    )

    misconfigured = [
        name
        for name, configured, expected in (
            ("assistant_provider", settings.assistant_provider, REQUIRED_ASSISTANT_PROVIDER),
            ("stt_provider", settings.stt_provider, REQUIRED_STT_PROVIDER),
            ("tts_provider", settings.tts_provider, REQUIRED_TTS_PROVIDER),
        )
        if configured != expected
    ]
    if misconfigured:
        # Never crash over this -- mock providers remain a legitimate dev
        # mode -- but never silently pretend real voice interaction is
        # ready either. GET /api/v1/runtime/readiness is the authoritative,
        # machine-checkable version of this same warning.
        logger.warning(
            "%s Not real-provider-configured: %s. Check GET /api/v1/runtime/readiness.",
            NOT_CONFIGURED_MESSAGE,
            ", ".join(misconfigured),
        )

    if settings.effective_host == "0.0.0.0":
        logger.warning(
            "binding to 0.0.0.0:%d — reachable from other devices on this "
            "LAN. Never port-forward this port; use Tailscale Serve for "
            "private remote access instead.",
            settings.backend_port,
        )
    else:
        logger.info(
            "binding to %s:%d (localhost-only)", settings.effective_host, settings.backend_port
        )

    db = build_database(settings)
    await db.connect()
    app.state.db = db

    ws_manager = ConnectionManager()
    app.state.ws_manager = ws_manager

    event_bus = EventBus(db, ws_manager)
    app.state.event_bus = event_bus

    memory_service = MemoryService(
        db.conn,
        vault_path=settings.obsidian_vault_path,
        sqlite_vec_enabled=settings.sqlite_vec_enabled,
    )
    app.state.memory_service = memory_service
    startup_index = await memory_service.reindex_vault()
    if not startup_index.vault_missing:
        logger.info(
            "vault indexed at startup: %d indexed, %d unchanged, %d removed",
            startup_index.indexed,
            startup_index.unchanged,
            startup_index.removed,
        )

    scheduler = build_scheduler(memory_service, timezone=settings.tars_timezone)
    scheduler.start()
    app.state.scheduler = scheduler

    action_ws_manager = ConnectionManager()
    app.state.action_ws_manager = action_ws_manager
    frontend_bridge = FrontendCommandBridge(action_ws_manager)
    app.state.frontend_bridge = frontend_bridge

    from app.latency_store import LatencyTraceStore

    latency_trace_store = LatencyTraceStore(db.conn)
    app.state.latency_trace_store = latency_trace_store
    from app.voice_telemetry import VoiceTraceStore

    app.state.voice_trace_store = VoiceTraceStore(db.conn)

    try:
        app.state.assistant_provider = build_assistant_provider(
            settings, trace_store=latency_trace_store
        )
        logger.info("assistant provider: %s", settings.assistant_provider)
    except Exception:
        logger.exception(
            "failed to construct assistant provider '%s' — falling back to mock",
            settings.assistant_provider,
        )
        app.state.assistant_provider = MockAssistantProvider()

    chart_analysis_service = ChartAnalysisService(
        build_chart_assistant_provider(settings), trace_store=latency_trace_store
    )
    app.state.chart_analysis_service = chart_analysis_service

    from assistant.chart_watch import ChartWatchService
    from assistant.hot_chart_state_store import HotChartStateStore

    app.state.chart_watch_service = ChartWatchService(
        # A separate ChartAnalysisService instance (same provider factory,
        # no trace_store) rather than reusing chart_analysis_service above:
        # background-watcher vision calls are a distinct traffic class from
        # user-triggered ones, and Phase A's request_traces table only
        # tracks the kind="chart_analysis" latency the user is waiting on --
        # conflating watcher-driven calls into the same trace stream would
        # skew that baseline. Revisit if Phase G's provider routing wants
        # watcher-call telemetry too.
        ChartAnalysisService(build_chart_assistant_provider(settings)),
        HotChartStateStore(db.conn),
    )

    strategy_provider = build_strategy_provider(settings.quant_brain_base_url)
    app.state.strategy_provider = strategy_provider
    trading_context_builder = TradingContextBuilder(EventService(db.conn), strategy_provider)
    app.state.trading_context_builder = trading_context_builder

    from pathlib import Path

    from skill_registry.manager import SkillManager

    skill_manager = SkillManager(
        db.conn,
        settings.obsidian_vault_path,
        Path(__file__).resolve().parents[1] / "data" / "catalog" / "hermes-skills-index.json.gz",
    )
    app.state.skill_manager = skill_manager
    # Ephemeral, in-process only (mirrors ClaudeCodeProvider._sessions):
    # TarsOrchestrator is constructed fresh per request (see app/deps.py's
    # get_orchestrator), so a pending "install this skill?" confirmation
    # needs to live somewhere that survives across the confirm-turn's
    # separate request -- app.state is the one thing that does. Lost on
    # restart, same as any other in-flight confirmation token.
    app.state.pending_skill_confirmations = {}

    action_registry = build_skill_registry(
        memory_service=memory_service,
        frontend_bridge=frontend_bridge,
        chart_analysis_service=chart_analysis_service,
        trading_context_builder=trading_context_builder,
        skill_manager=skill_manager,
    )
    app.state.action_registry = action_registry
    action_runtime = ActionRuntime(
        ActionStore(db.conn),
        action_registry,
        permission_engine=PermissionEngine(),
        broadcaster=action_ws_manager,
    )
    await action_runtime.initialize()
    app.state.action_runtime = action_runtime

    # Two independently-built agent systems, reconciled at integration time
    # (see the merge commit): `agent_job_runtime` is the agent-runtime
    # stream's durable, provider-neutral LLM-decision-loop job runtime;
    # `agent_runtime` (below) is the TARS core stream's simpler bounded
    # ON_DEMAND/SCHEDULED/CONTINUOUS worker framework with three concrete
    # trading agents. Both are real, tested, and kept side by side rather
    # than one being discarded -- see app/routers/agent_runtime.py
    # (/api/v1/agent-runtime) vs. app/routers/agents.py (/api/v1/agents).
    agent_job_runtime = AgentJobRuntime(
        AgentJobStore(db.conn),
        action_runtime,
        IntelligenceProviderRegistry(),
        strategy_boundary=QuantBrainBoundary(),
    )
    recovered_jobs = await agent_job_runtime.initialize()
    if recovered_jobs:
        logger.warning(
            "%d interrupted agent job(s) require explicit recovery", len(recovered_jobs)
        )
    app.state.agent_job_runtime = agent_job_runtime
    scheduler.add_job(
        agent_job_runtime.run_due,
        "interval",
        seconds=1,
        id="agent_due_jobs",
        max_instances=1,
        coalesce=True,
    )
    plan_runtime = PlanRuntime(PlanStore(db.conn), action_runtime)
    await plan_runtime.initialize()
    app.state.plan_runtime = plan_runtime

    agent_runtime = AgentRuntime(AgentRunStore(db.conn))
    app.state.agent_runtime = agent_runtime
    app.state.agents = {
        "chart_analysis_agent": ChartAnalysisAgent(action_runtime, memory_service),
        "trading_workspace_agent": TradingWorkspaceAgent(action_runtime),
        "setup_watch_agent": SetupWatchAgent(
            trading_context_builder,
            memory_service,
            config=AgentConfig(
                mode=AgentMode.CONTINUOUS,
                interval_seconds=settings.setup_watch_agent_interval_seconds,
                timeout_seconds=15.0,
            ),
        ),
    }
    if settings.setup_watch_agent_enabled:
        await agent_runtime.start_continuous(app.state.agents["setup_watch_agent"])
        logger.info(
            "setup_watch_agent started (interval=%.1fs, deterministic state only)",
            settings.setup_watch_agent_interval_seconds,
        )

    generator: MockEventGenerator | None = None
    if settings.use_mock_trading_events:
        generator = MockEventGenerator(
            emit=event_bus.emit, interval_seconds=settings.mock_event_interval_seconds
        )
        generator.start()
        logger.info("mock trading-event generator started")
    app.state.mock_generator = generator

    voice_providers = VoiceProviders()
    app.state.voice_providers = voice_providers
    voice_load_task = asyncio.create_task(voice_providers.load(settings))
    logger.info("voice provider loading started in background")

    # One long-lived turn owner is required for in-flight duplicate joining
    # and completed-turn replay.  Request-scoped construction would silently
    # defeat that invariant.
    from assistant.conversation_store import ConversationStore
    from assistant.router import AssistantRouter
    from assistant.turn_controller import AssistantTurnController
    from orchestrator.orchestrator import TarsOrchestrator

    conversation_store = ConversationStore(db.conn)
    assistant_router = AssistantRouter(
        event_service=EventService(db.conn),
        conversation_store=conversation_store,
        provider=app.state.assistant_provider,
        memory_service=memory_service,
        trace_store=latency_trace_store,
    )
    orchestrator = TarsOrchestrator(
        assistant_router=assistant_router,
        action_runtime=action_runtime,
        memory_service=memory_service,
        conversation_store=conversation_store,
        agent_runtime=agent_runtime,
        agents=app.state.agents,
        skill_manager=skill_manager,
        pending_skill_confirmations=app.state.pending_skill_confirmations,
    )
    app.state.turn_controller = AssistantTurnController(
        settings=settings,
        provider=app.state.assistant_provider,
        assistant_router=assistant_router,
        orchestrator=orchestrator,
        action_runtime=action_runtime,
        conversation_store=conversation_store,
        hot_chart_state_store=HotChartStateStore(db.conn),
        voice_providers=voice_providers,
        voice_trace_store=app.state.voice_trace_store,
    )

    try:
        yield
    finally:
        voice_load_task.cancel()
        if settings.setup_watch_agent_enabled:
            await agent_runtime.stop_continuous("setup_watch_agent")
        scheduler.shutdown(wait=False)
        if generator is not None:
            await generator.stop()
        await db.close()


def create_app() -> FastAPI:
    app = FastAPI(title="TARS Backend", version="0.1.0", lifespan=lifespan)
    settings = get_settings()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Outermost middleware (added last): enforces MAX_BODY_BYTES by
    # counting bytes as they stream in, so it catches chunked-transfer
    # bodies (no Content-Length) as well as declared-length ones. See
    # app/body_limit.py.
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=MAX_BODY_BYTES)

    app.include_router(health.router)
    app.include_router(runtime.router)
    app.include_router(diagnostics.router)
    app.include_router(events.router)
    app.include_router(assistant.router)
    app.include_router(chart_watch.router)
    app.include_router(memory.router)
    app.include_router(voice.router)
    app.include_router(ws.router)
    app.include_router(actions.router)
    app.include_router(action_plans.router)
    app.include_router(agents_router.router)
    app.include_router(agent_runtime_router.router)

    return app


app = create_app()
