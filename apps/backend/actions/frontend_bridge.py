"""Bridge for actions whose physical execution can only happen in the
connected native/renderer process (embedded browser DOM, native screen/UIA
capture) -- the Python backend has no direct access to that process's
memory space.

The backend remains the sole permission/classification/audit authority:
PermissionEngine.classify() and the skill's validate() run *before*
anything is dispatched here, exactly as for any other skill. This bridge
only carries an already-authorized command out to the one process that can
perform it, and waits for a truthful, real report of what happened. It
never fabricates a result and never lets the frontend re-decide risk,
confirmation, or success -- see BaseSkill.execute()'s contract.
"""
from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from app.ws_manager import ConnectionManager


class FrontendBridgeError(RuntimeError):
    """Raised when a dispatched command could not be completed by the
    frontend -- e.g. no client connected, or it did not report back in
    time. Callers must surface this as a real failure, never as SUCCEEDED."""


class FrontendCommandBridge:
    def __init__(self, broadcaster: ConnectionManager) -> None:
        self._broadcaster = broadcaster
        self._pending: dict[UUID, asyncio.Future[dict[str, Any]]] = {}

    async def dispatch(
        self,
        request_id: UUID,
        skill: str,
        action: str,
        arguments: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        """Sends the already-authorized command to every connected client and
        waits up to `timeout` seconds for that request_id's report. Returns
        the reported payload's `data` dict on success; raises
        FrontendBridgeError on timeout or a reported failure."""
        if self._broadcaster.active_count == 0:
            raise FrontendBridgeError(
                f"No connected client to execute {skill}.{action}() -- nothing was performed."
            )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        try:
            await self._broadcaster.broadcast(
                {
                    "type": "frontend_command",
                    "request_id": str(request_id),
                    "skill": skill,
                    "action": action,
                    "arguments": arguments,
                }
            )
            try:
                payload = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            except TimeoutError as exc:
                raise FrontendBridgeError(
                    f"Frontend did not report a result for {skill}.{action}() in time"
                ) from exc
        finally:
            self._pending.pop(request_id, None)

        if not payload.get("success", False):
            raise FrontendBridgeError(
                str(payload.get("error") or f"Frontend reported failure executing {skill}.{action}()")
            )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def report(self, request_id: UUID, payload: dict[str, Any]) -> bool:
        """Called by the REST report endpoint when the frontend posts the
        real outcome of a dispatched command. Returns False if nothing was
        waiting for this request_id (already timed out, or unknown)."""
        future = self._pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(payload)
        return True
