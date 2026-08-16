"""FastAPI dependency accessors — thin wrappers pulling shared singletons
off `request.app.state`, set up once in `app.main`'s lifespan."""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

from app.config import Settings, get_settings
from app.db import Database
from app.event_bus import EventBus
from app.ws_manager import ConnectionManager
from events.service import EventService

if TYPE_CHECKING:
    from memory.service import MemoryService

    from assistant.provider import AssistantProvider
    from assistant.router import AssistantRouter


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
    )


def get_memory_service(request: Request) -> MemoryService:
    return request.app.state.memory_service
