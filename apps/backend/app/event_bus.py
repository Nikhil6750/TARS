"""Single choke point for "an event happened": validate against the frozen
contract, persist, then broadcast. Used identically by the mock generator
and the manual dev-injection endpoint so there is exactly one trading-event
pipeline — never a second, parallel path that could drift from the
contract or from what clients see over the WebSocket.
"""
from __future__ import annotations

import logging

from app.contracts import validate_trading_event
from app.db import Database
from app.schemas import TradingEvent
from app.ws_manager import ConnectionManager
from events.service import EventService

logger = logging.getLogger("tars.event_bus")


class EventBus:
    def __init__(self, db: Database, ws_manager: ConnectionManager):
        self._db = db
        self._ws_manager = ws_manager

    async def emit(self, event: TradingEvent) -> None:
        payload = event.to_contract_dict()
        validate_trading_event(payload)

        service = EventService(self._db.conn)
        change = await service.record_event(event)

        await self._ws_manager.broadcast(
            {
                "type": "trading_event",
                "event": payload,
                "active_state_change": change.action,
            }
        )
