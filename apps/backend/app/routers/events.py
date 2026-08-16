from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.contracts import ContractValidationError, validate_trading_event
from app.deps import get_event_bus, get_event_service
from app.event_bus import EventBus
from app.schemas import (
    Direction,
    EventSource,
    EventState,
    MockEventRequest,
    TradingEvent,
    ValidationStatus,
)
from events.service import DuplicateEventError, EventService

router = APIRouter(tags=["events"])


@router.get("/api/v1/events")
@router.get("/api/events")
@router.get("/api/v1/events/history")
@router.get("/api/events/history")
async def list_events(
    symbol: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    service: EventService = Depends(get_event_service),
) -> list[dict]:
    return await service.get_history(symbol=symbol, limit=limit)


@router.get("/api/v1/events/active")
@router.get("/api/events/active")
async def active_events(
    service: EventService = Depends(get_event_service),
) -> list[dict]:
    return await service.get_active_setups()


@router.post("/api/v1/events", status_code=201)
@router.post("/api/events", status_code=201)
async def post_event(
    request: Request,
    bus: EventBus = Depends(get_event_bus),
) -> dict:
    try:
        raw: Any = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON payload: {exc}") from exc

    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="Event payload must be a JSON object")

    try:
        validate_trading_event(raw)
    except ContractValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        event = TradingEvent(**raw)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        await bus.emit(event)
    except ContractValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DuplicateEventError as exc:
        raise HTTPException(
            status_code=409, detail=f"Event {exc.event_id} already exists"
        ) from exc

    return event.to_contract_dict()


@router.post("/api/v1/events/{event_id}/invalidate", status_code=200)
@router.post("/api/events/{event_id}/invalidate", status_code=200)
async def invalidate_event(
    event_id: str,
    request: Request,
    service: EventService = Depends(get_event_service),
    bus: EventBus = Depends(get_event_bus),
) -> dict:
    original = await service.get_by_id(event_id)
    if not original:
        raise HTTPException(status_code=404, detail=f"Event {event_id} not found")

    reason_codes: list[str] = ["MANUAL_INVALIDATION"]
    try:
        body: Any = await request.json()
        if isinstance(body, dict) and "reason_codes" in body and isinstance(body["reason_codes"], list):
            reason_codes = [str(r) for r in body["reason_codes"]]
    except Exception:
        pass

    # A new, unique event_id — never the original's. `trading_events.event_id`
    # is the primary key and persistence is plain append-only INSERT, so
    # reusing the original id would be rejected as a duplicate instead of
    # appending a new historical event. The original is preserved for
    # audit/correlation purposes as a reason code instead, since the frozen
    # trading-event contract has no dedicated field for it.
    invalidated_event = TradingEvent(
        source=EventSource.manual,
        symbol=original["symbol"],
        strategy_id=original.get("strategy_id"),
        state=EventState.SETUP_INVALIDATED,
        direction=Direction(original["direction"]) if original.get("direction") else None,
        entry=original.get("entry"),
        stop_loss=original.get("stop_loss"),
        take_profit=original.get("take_profit"),
        risk_reward=original.get("risk_reward"),
        risk_percent=original.get("risk_percent"),
        validation_status=ValidationStatus.INVALID,
        reason_codes=[*reason_codes, f"ORIGINAL_EVENT_ID:{original['event_id']}"],
        warnings=original.get("warnings", []),
        expires_at=original.get("expires_at"),
    )

    try:
        await bus.emit(invalidated_event)
    except ContractValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DuplicateEventError as exc:
        raise HTTPException(
            status_code=409, detail=f"Event {exc.event_id} already exists"
        ) from exc

    return invalidated_event.to_contract_dict()


@router.post("/api/v1/dev/mock-event", status_code=201)
@router.post("/api/dev/mock-event", status_code=201)
async def create_mock_event(
    body: MockEventRequest,
    bus: EventBus = Depends(get_event_bus),
) -> dict:
    """Developer/coordinator-authored test event, source='manual' per
    contracts/trading-event.schema.json. Never used for the live mock
    generator's own traffic — see events/generator.py for that."""
    event = TradingEvent(
        source=EventSource.manual,
        symbol=body.symbol,
        strategy_id=body.strategy_id,
        state=body.state,
        direction=body.direction,
        entry=body.entry,
        stop_loss=body.stop_loss,
        take_profit=body.take_profit,
        risk_reward=body.risk_reward,
        risk_percent=body.risk_percent,
        validation_status=body.validation_status,
        reason_codes=body.reason_codes,
        warnings=body.warnings,
        expires_at=body.expires_at,
    )
    try:
        await bus.emit(event)
    except ContractValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DuplicateEventError as exc:
        raise HTTPException(
            status_code=409, detail=f"Event {exc.event_id} already exists"
        ) from exc
    return event.to_contract_dict()
