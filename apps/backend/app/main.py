from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import build_database
from app.event_bus import EventBus
from app.routers import assistant, events, health, ws
from app.ws_manager import ConnectionManager
from assistant.factory import build_assistant_provider
from assistant.providers.mock import MockAssistantProvider
from events.generator import MockEventGenerator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tars.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    db = build_database(settings)
    await db.connect()
    app.state.db = db

    ws_manager = ConnectionManager()
    app.state.ws_manager = ws_manager

    event_bus = EventBus(db, ws_manager)
    app.state.event_bus = event_bus

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

    try:
        yield
    finally:
        if generator is not None:
            await generator.stop()
        await db.close()


def create_app() -> FastAPI:
    app = FastAPI(title="TARS Backend", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(events.router)
    app.include_router(assistant.router)
    app.include_router(ws.router)

    return app


app = create_app()
