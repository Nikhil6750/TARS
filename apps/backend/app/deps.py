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
    from actions.plan_runtime import PlanRuntime
    from actions.runtime import ActionRuntime
    from app.voice_state import VoiceProviders
    from assistant.provider import AssistantProvider
    from assistant.router import AssistantRouter
    from memory.service import MemoryService


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
    )


def get_memory_service(request: Request) -> MemoryService:
    return request.app.state.memory_service


def get_voice_providers(request: Request) -> VoiceProviders:
    return request.app.state.voice_providers


def get_action_runtime(request: Request) -> ActionRuntime:
    return request.app.state.action_runtime


def get_plan_runtime(request: Request) -> PlanRuntime:
    return request.app.state.plan_runtime
