"""Enforces a hard request-body size ceiling at the ASGI layer.

A `Content-Length`-only check (comparing the declared header against the
limit) is not a real enforcement: a client using chunked transfer encoding
never sends `Content-Length` at all, so that check is a no-op for exactly
the request shape most likely to be used to smuggle an oversized body past
it. This middleware instead counts bytes as the ASGI server delivers them
via `receive()` — which is how uvicorn represents both Content-Length- and
chunked-framed bodies alike — and aborts as soon as the running total
exceeds the limit, before the body is ever fully buffered by anything
downstream (FastAPI's `Request.body()`/`.json()` included).
"""
from __future__ import annotations

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestBodyTooLarge(BaseException):
    """Deliberately a `BaseException`, not an `Exception`, subclass: it must
    propagate through any `except Exception` a route handler happens to
    wrap around body reading (e.g. a JSON-parse try/except) rather than be
    swallowed and reported as an unrelated 4xx, the same reason
    `asyncio.CancelledError` isn't an `Exception` subclass either."""


class MaxBodySizeMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        total = 0

        async def guarded_receive() -> Message:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body") or b"")
                if total > self.max_bytes:
                    raise RequestBodyTooLarge()
            return message

        response_started = False

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, guarded_receive, guarded_send)
        except RequestBodyTooLarge:
            if response_started:
                # Headers already sent by the time we detected the
                # overflow — too late to send a fresh 413, let the
                # connection end rather than violate the ASGI response
                # lifecycle by sending a second `http.response.start`.
                return
            response = PlainTextResponse("Payload Too Large", status_code=413)
            await response(scope, receive, send)
