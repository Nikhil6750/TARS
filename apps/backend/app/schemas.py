"""Pydantic models mirroring contracts/*.schema.json for typed FastAPI I/O.

These are a typed convenience layer over the canonical JSON Schemas in
`contracts/` — they exist for FastAPI request/response typing, OpenAPI docs,
and IDE support. The canonical validation (the check that actually gates
"is this event valid") is `app.contracts.validate_trading_event`, run
against the frozen schema file itself, so this module cannot silently drift
into accepting something the contract forbids.
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator


class EventSource(str, Enum):
    mock = "mock"
    quant_brain = "quant_brain"
    manual = "manual"


class EventState(str, Enum):
    IDLE = "IDLE"
    SETUP_DEVELOPING = "SETUP_DEVELOPING"
    SETUP_VALID = "SETUP_VALID"
    SETUP_INVALIDATED = "SETUP_INVALIDATED"
    RISK_WARNING = "RISK_WARNING"
    SYSTEM_WARNING = "SYSTEM_WARNING"


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


class ValidationStatus(str, Enum):
    PENDING = "PENDING"
    VALID = "VALID"
    INVALID = "INVALID"
    EXPIRED = "EXPIRED"


# States that represent an actively-tracked setup for a symbol, as opposed
# to a terminal/system state. Used by the active-state calculation — kept
# here, next to the enum it's derived from, rather than scattered.
ACTIVE_STATES = {EventState.SETUP_DEVELOPING, EventState.SETUP_VALID}


class TradingEvent(BaseModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    event_id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: EventSource
    symbol: str = Field(min_length=1)
    strategy_id: str | None = None
    state: EventState
    direction: Direction | None = None
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    risk_reward: float | None = None
    risk_percent: float | None = Field(default=None, ge=0)
    validation_status: ValidationStatus
    reason_codes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None

    def to_contract_dict(self) -> dict:
        """Serializes exactly as contracts/trading-event.schema.json expects
        (ISO-8601 strings, plain str uuid, no extra fields)."""
        data = self.model_dump(mode="json")
        return data


class MockEventRequest(BaseModel):
    """Body for POST /api/v1/dev/mock-event — lets a developer/coordinator
    inject a manual test event. Still validated against the frozen contract."""

    symbol: str = Field(min_length=1)
    state: EventState
    validation_status: ValidationStatus = ValidationStatus.PENDING
    strategy_id: str | None = None
    direction: Direction | None = None
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    risk_reward: float | None = None
    risk_percent: float | None = Field(default=None, ge=0)
    reason_codes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None

    @field_validator("symbol")
    @classmethod
    def _uppercase_symbol(cls, v: str) -> str:
        return v.strip().upper()


class MessageRole(str, Enum):
    user = "user"
    assistant = "assistant"
    system = "system"


class InputMode(str, Enum):
    text = "text"
    voice = "voice"


class MessageProviders(BaseModel):
    stt: str | None = None
    assistant: str | None = None
    tts: str | None = None


class AssistantMessage(BaseModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    message_id: UUID = Field(default_factory=uuid4)
    conversation_id: UUID
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    role: MessageRole
    content: str
    input_mode: InputMode
    audio_ref: str | None = None
    related_event_id: str | None = None
    intent: str | None = None
    providers: MessageProviders = Field(default_factory=MessageProviders)
    error: str | None = None

    def to_contract_dict(self) -> dict:
        return self.model_dump(mode="json")


class TextQueryRequest(BaseModel):
    conversation_id: UUID | None = None
    text: str = Field(min_length=1)


class HealthResponse(BaseModel):
    status: Literal["ok"]
    tars_env: str
    database: Literal["ok", "error"]
    assistant_provider: str
    stt_provider: str
    tts_provider: str
    wake_word_provider: str
