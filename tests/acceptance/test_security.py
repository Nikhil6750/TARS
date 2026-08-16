from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from tools.tars_test_client import TarsTestClient
from tools.security_checks import find_live_execution_operations


pytestmark = pytest.mark.acceptance


def assert_rejected(response: httpx.Response) -> None:
    assert response.status_code in {400, 413, 422}, response.text


def test_unexpected_event_field_is_rejected(client: TarsTestClient) -> None:
    fixture = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "malformed"
            / "extra_property.json"
        ).read_text(encoding="utf-8")
    )
    response = client.http.post(client._url(client.routes.events), json=fixture)
    assert_rejected(response)


def test_oversized_payload_is_rejected(
    client: TarsTestClient, valid_event: dict[str, object]
) -> None:
    valid_event["warnings"] = ["X" * 1_048_576]
    response = client.http.post(client._url(client.routes.events), json=valid_event)
    assert_rejected(response)


@pytest.mark.parametrize(
    "mutation",
    (
        {"content": None},
        {"input_mode": "telepathy"},
        {"unexpected": "field"},
    ),
)
def test_malformed_assistant_request_is_rejected(
    client: TarsTestClient, mutation: dict[str, object]
) -> None:
    request = client.user_message("test")
    request.update(mutation)
    response = client.http.post(client._url(client.routes.assistant), json=request)
    assert_rejected(response)


def test_secret_is_not_reflected_in_error_response(client: TarsTestClient) -> None:
    sentinel = os.environ["TARS_SECRET_SENTINEL"]
    request = client.user_message(sentinel)
    request["unexpected"] = "force rejection"
    response = client.http.post(client._url(client.routes.assistant), json=request)
    assert_rejected(response)
    assert sentinel not in response.text


def test_no_live_trade_execution_surface(client: TarsTestClient) -> None:
    openapi = client.http.get(client._url("/openapi.json"))
    openapi.raise_for_status()
    document = openapi.json()
    discovered = find_live_execution_operations(document)
    assert not discovered, f"live trade-execution-like endpoints exist: {discovered}"

    for path in (
        "/api/trades/execute",
        "/api/orders",
        "/api/execution",
        "/api/broker/orders",
        "/execute",
    ):
        response = client.http.get(client._url(path))
        assert response.status_code == 404, f"route appears to exist: GET {path}"
