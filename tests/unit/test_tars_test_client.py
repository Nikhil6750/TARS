from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from tools.tars_test_client import (
    ContractViolation,
    GroundingViolation,
    Routes,
    TarsTestClient,
)

ROOT = Path(__file__).resolve().parents[2]
VALID_EVENT = json.loads(
    (ROOT / "tests" / "fixtures" / "valid" / "setup_valid.json").read_text(
        encoding="utf-8"
    )
)


def response(data: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=data)


def test_send_event_validates_before_network() -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return response({"accepted": True})

    with (
        TarsTestClient(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ContractViolation),
    ):
        client.send_event({"state": "SETUP_VALID"})
    assert not called


def test_queries_validate_public_event_contracts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/events/active"
        return response({"active_events": [VALID_EVENT]})

    with TarsTestClient(transport=httpx.MockTransport(handler)) as client:
        assert client.active_events() == [VALID_EVENT]


def test_symbol_history_uses_public_query_filter() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/events/history"
        assert request.url.params["symbol"] == "ES"
        return response([VALID_EVENT])

    with TarsTestClient(transport=httpx.MockTransport(handler)) as client:
        assert client.history_for_symbol("ES") == [VALID_EVENT]


def test_memory_search_preserves_public_source_identifier() -> None:
    expected = {
        "source": "vault",
        "source_id": "Research/Setup.md",
        "title": "Setup",
        "snippet": "attention marker",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/memory/search"
        assert request.url.params["q"] == "attention"
        assert request.url.params["source"] == "vault"
        return response([expected])

    with TarsTestClient(transport=httpx.MockTransport(handler)) as client:
        assert client.search_memory("attention", source="vault") == [expected]


def test_configurable_routes_do_not_require_backend_imports() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return response({"status": "ok"})

    routes = Routes(health="/status")
    with TarsTestClient(routes=routes, transport=httpx.MockTransport(handler)) as client:
        assert client.health()["status"] == "ok"
    assert seen == ["/status"]


def test_grounding_rejects_unknown_numeric_claim() -> None:
    assistant = TarsTestClient.user_message("Entry is 9999.5")
    assistant["role"] = "assistant"
    assistant["providers"]["assistant"] = "mock"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("active"):
            return response([VALID_EVENT])
        if request.url.path.endswith("history"):
            return response([])
        return response(assistant)

    with (
        TarsTestClient(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(GroundingViolation, match="absent from state"),
    ):
        client.verify_grounded_answer("What is ES entry?", "ES")


def test_grounding_requires_uncertainty_when_symbol_is_missing() -> None:
    assistant = TarsTestClient.user_message("EURUSD entry is ready")
    assistant["role"] = "assistant"
    assistant["providers"]["assistant"] = "mock"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(("active", "history")):
            return response([])
        return response(assistant)

    with (
        TarsTestClient(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(GroundingViolation, match="did not disclose"),
    ):
        client.verify_grounded_answer("What is EURUSD entry?", "EURUSD")


def test_websocket_url_tracks_http_security() -> None:
    with TarsTestClient("https://tars.example.test/base") as client:
        assert client.websocket_url == "wss://tars.example.test/base/ws"
