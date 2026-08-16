from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.contracts import ContractValidationError
from app.deps import get_event_bus, get_event_service
from app.event_bus import EventBus
from app.schemas import EventSource, MockEventRequest, TradingEvent
from events.service import EventService

router = APIRouter(prefix="/api/v1", tags=["events"])


@router.get("/events")
async def list_events(
    symbol: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    service: EventService = Depends(get_event_service),
) -> list[dict]:
    return await service.get_history(symbol=symbol, limit=limit)


@router.get("/events/active")
async def active_events(
    service: EventService = Depends(get_event_service),
) -> list[dict]:
    return await service.get_active_setups()


@router.post("/dev/mock-event", status_code=201)
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
    return event.to_contract_dict()
