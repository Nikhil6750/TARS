from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

import pytest

from tools.tars_test_client import TarsTestClient

pytestmark = pytest.mark.acceptance
ONE_MIB = 1_048_576


@dataclass(frozen=True)
class RawResponse:
    status_code: int
    headers: bytes


def oversized_json(event: dict[str, Any]) -> bytes:
    event["warnings"] = ["PAYLOAD-LIMIT-REGRESSION-" + "X" * ONE_MIB]
    # If the byte limiter is broken, strict schema validation must still keep
    # this request from persisting and polluting later acceptance scenarios.
    event["certification_extra_field"] = True
    payload = json.dumps(event, separators=(",", ":")).encode("utf-8")
    assert len(payload) > ONE_MIB
    return payload


def raw_post(
    url: str,
    body: bytes,
    framing: Literal["content-length", "no-length", "chunked"],
    timeout: float = 5.0,
) -> RawResponse:
    """Send an independently framed HTTP request over a public TCP socket."""

    parsed = urlsplit(url)
    assert parsed.scheme == "http", "raw payload certification expects local HTTP"
    assert parsed.hostname is not None
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"

    version = "HTTP/1.0" if framing == "no-length" else "HTTP/1.1"
    headers = [
        f"POST {path} {version}",
        f"Host: {parsed.hostname}:{port}",
        "Content-Type: application/json",
        "Connection: close",
    ]
    if framing == "content-length":
        headers.append(f"Content-Length: {len(body)}")
        wire_body = body
    elif framing == "chunked":
        headers.append("Transfer-Encoding: chunked")
        chunks = [body[index : index + 65_536] for index in range(0, len(body), 65_536)]
        wire_body = b"".join(
            f"{len(chunk):X}\r\n".encode("ascii") + chunk + b"\r\n"
            for chunk in chunks
        ) + b"0\r\n\r\n"
    else:
        # HTTP/1.0 permits request bodies delimited by closing the write side.
        # Deliberately send neither Content-Length nor Transfer-Encoding.
        wire_body = body

    request = "\r\n".join(headers).encode("ascii") + b"\r\n\r\n" + wire_body
    received = bytearray()
    with socket.create_connection((parsed.hostname, port), timeout=timeout) as connection:
        connection.settimeout(timeout)
        try:
            connection.sendall(request)
            connection.shutdown(socket.SHUT_WR)
        except (BrokenPipeError, ConnectionResetError):
            # A limit-aware server may reject and close before the sender
            # finishes writing; its response is still the evidence we need.
            pass
        while b"\r\n\r\n" not in received:
            chunk = connection.recv(4096)
            if not chunk:
                break
            received.extend(chunk)
            assert len(received) <= 65_536, "response headers exceeded safe bound"

    header_block = bytes(received).split(b"\r\n\r\n", 1)[0]
    status_line = header_block.split(b"\r\n", 1)[0]
    parts = status_line.split()
    assert len(parts) >= 2, f"invalid HTTP response: {status_line!r}"
    return RawResponse(status_code=int(parts[1]), headers=header_block)


def event_url(client: TarsTestClient) -> str:
    return urljoin(client.base_url, client.routes.events.lstrip("/"))


def test_oversized_payload_with_content_length_is_413(
    client: TarsTestClient, valid_event: dict[str, Any]
) -> None:
    response = raw_post(event_url(client), oversized_json(valid_event), "content-length")
    assert response.status_code == 413


def test_oversized_payload_without_content_length_is_rejected(
    client: TarsTestClient, valid_event: dict[str, Any]
) -> None:
    response = raw_post(event_url(client), oversized_json(valid_event), "no-length")
    assert 400 <= response.status_code < 500


def test_oversized_chunked_stream_is_413(
    client: TarsTestClient, valid_event: dict[str, Any]
) -> None:
    payload = oversized_json(valid_event)

    def chunks():
        for index in range(0, len(payload), 65_536):
            yield payload[index : index + 65_536]

    response = client.http.post(
        event_url(client),
        content=chunks(),
        headers={"Content-Type": "application/json"},
    )
    assert "content-length" not in response.request.headers
    assert response.request.headers["transfer-encoding"] == "chunked"
    assert response.status_code == 413
