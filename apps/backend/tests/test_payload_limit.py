"""Regression tests for the certification blocker where the 1 MiB request
body ceiling only inspected the `Content-Length` header — a no-op against
chunked transfer encoding, which never sends that header. `MaxBodySizeMiddleware`
(app/body_limit.py) instead counts bytes as they stream through `receive()`,
so it catches an oversized body regardless of how it was framed.
"""
from __future__ import annotations

from app.main import MAX_BODY_BYTES


def _chunks(total_bytes: int, chunk_size: int = 64 * 1024):
    """A body iterator with no known total length — forces httpx to send
    the request with `Transfer-Encoding: chunked` and no `Content-Length`
    header, exactly the shape the old Content-Length-only check missed."""
    remaining = total_bytes
    while remaining > 0:
        size = min(chunk_size, remaining)
        yield b"x" * size
        remaining -= size


def test_normal_payload_passes(client):
    resp = client.post(
        "/api/v1/dev/mock-event",
        json={"symbol": "ES", "state": "IDLE", "validation_status": "PENDING"},
    )
    assert resp.status_code == 201


def test_content_length_oversized_payload_is_rejected(client):
    body = b"{" + b'"padding": "' + b"x" * (MAX_BODY_BYTES + 1) + b'"}'
    assert len(body) > MAX_BODY_BYTES

    resp = client.post(
        "/api/v1/events",
        content=body,
        headers={"content-length": str(len(body)), "content-type": "application/json"},
    )
    assert resp.status_code == 413


def test_chunked_oversized_payload_is_rejected_without_content_length(client):
    resp = client.post(
        "/api/v1/events",
        content=_chunks(MAX_BODY_BYTES + 1024),
        headers={"content-type": "application/json"},
    )
    request = resp.request
    assert "content-length" not in {k.lower() for k in request.headers.keys()}
    assert resp.status_code == 413


def test_chunked_payload_under_limit_reaches_the_route(client):
    # Small chunked body with no Content-Length must behave like any other
    # request — rejected for being invalid JSON (422), never 413.
    resp = client.post(
        "/api/v1/events",
        content=_chunks(1024),
        headers={"content-type": "application/json"},
    )
    request = resp.request
    assert "content-length" not in {k.lower() for k in request.headers.keys()}
    assert resp.status_code == 422
