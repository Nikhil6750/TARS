from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.ws_manager import ConnectionManager
from events.service import EventService

logger = logging.getLogger("tars.ws")

router = APIRouter()


@router.websocket("/ws/events")
async def events_ws(websocket: WebSocket) -> None:
    manager: ConnectionManager = websocket.app.state.ws_manager
    db = websocket.app.state.db
    await manager.connect(websocket)
    try:
        # Snapshot on connect so a client never has to wait for the next
        # emitted event to know current active state.
        service = EventService(db.conn)
        active = await service.get_active_setups()
        await websocket.send_json({"type": "active_snapshot", "events": active})

        while True:
            # Clients are not required to send anything; this keeps the
            # connection open and lets the server notice a clean close.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)
