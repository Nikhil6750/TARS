"""WebSocket connection registry + broadcast for /ws/events.

Broadcasts `trading-event` messages (contract-valid dicts) and lightweight
companion-state messages to every connected client. A dead connection is
dropped on the next broadcast rather than eagerly monitored — this backend
targets a handful of concurrent clients (laptop, iPhone, later ESP32), not
high connection churn.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger("tars.ws")


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    @property
    def active_count(self) -> int:
        return len(self._connections)

    async def broadcast(self, message: dict[str, Any]) -> None:
        if not self._connections:
            return
        payload = json.dumps(message)
        dead: list[WebSocket] = []
        for connection in self._connections:
            try:
                await connection.send_text(payload)
            except Exception:
                dead.append(connection)
        for connection in dead:
            self._connections.discard(connection)
