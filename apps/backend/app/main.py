from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import build_database
from app.event_bus import EventBus
from app.observability import configure_tracing
from app.routers import assistant, events, health, memory, voice, ws
from app.scheduler import build_scheduler
from app.voice_state import VoiceProviders
from app.ws_manager import ConnectionManager
from assistant.factory import build_assistant_provider
from assistant.providers.mock import MockAssistantProvider
from events.generator import MockEventGenerator
from memory.service import MemoryService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tars.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_tracing("tars-backend", settings.otel_exporter_otlp_endpoint)

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

    try:
        app.state.assistant_provider = build_assistant_provider(settings)
        logger.info("assistant provider: %s", settings.assistant_provider)
    except Exception:
        logger.exception(
            "failed to construct assistant provider '%s' — falling back to mock",
            settings.assistant_provider,
        )
        app.state.assistant_provider = MockAssistantProvider()

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

    try:
        yield
    finally:
        voice_load_task.cancel()
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

    app.include_router(health.router)
    app.include_router(events.router)
    app.include_router(assistant.router)
    app.include_router(memory.router)
    app.include_router(voice.router)
    app.include_router(ws.router)

    return app


app = create_app()
