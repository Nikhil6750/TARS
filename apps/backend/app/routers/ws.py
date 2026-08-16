from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.ws_manager import ConnectionManager
from events.service import EventService

logger = logging.getLogger("tars.ws")

router = APIRouter()


async def _handle_ws(websocket: WebSocket) -> None:
    manager: ConnectionManager = websocket.app.state.ws_manager
    db = websocket.app.state.db
    await manager.connect(websocket)
    try:
        service = EventService(db.conn)
        active = await service.get_active_setups()
        await websocket.send_json({"type": "active_snapshot", "events": active})

        while True:
            raw_text = await websocket.receive_text()
            try:
                data = json.loads(raw_text)
                if isinstance(data, dict) and data.get("type") == "ping":
                    await websocket.send_json({"type": "pong", "timestamp": data.get("timestamp")})
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)


@router.websocket("/ws/events")
async def events_ws_events(websocket: WebSocket) -> None:
    await _handle_ws(websocket)


@router.websocket("/ws")
async def events_ws_default(websocket: WebSocket) -> None:
    await _handle_ws(websocket)
