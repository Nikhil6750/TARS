from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest
from websockets.asyncio.client import connect

from tools.tars_test_client import ContractViolation, TarsTestClient

from .conftest import poll_until

pytestmark = pytest.mark.acceptance


async def receive_event(
    websocket: Any, client: TarsTestClient, event_id: str, state: str
) -> dict[str, Any]:
    async with asyncio.timeout(client.timeout_seconds):
        while True:
            payload = json.loads(await websocket.recv())
            candidate = client._unwrap_websocket_event(payload)
            if not {"schema_version", "event_id", "state"} <= candidate.keys():
                continue
            event = client.contracts.validate_event(candidate)
            if event["event_id"] == event_id and event["state"] == state:
                return event


def test_backend_and_frontend_were_started_and_health_works(
    client: TarsTestClient,
) -> None:
    assert os.getenv("TARS_ACCEPTANCE_PROCESSES_STARTED") == "1"
    health = client.health()
    assert str(health.get("status", "")).casefold() in {"ok", "healthy", "ready"}
    assert os.getenv("TARS_ACCEPTANCE_ZERO_PAID_KEYS") == "1"
    assert os.getenv("TARS_ACCEPTANCE_VAULT_SOURCE_ID")
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "FISH_AUDIO_API_KEY"):
        assert not os.getenv(key)


def test_event_lifecycle_reaches_two_clients_and_persists(
    client: TarsTestClient, valid_event: dict[str, Any]
) -> None:
    async def scenario() -> None:
        async with connect(client.websocket_url) as first, connect(
            client.websocket_url
        ) as second:
            first_valid = asyncio.create_task(
                receive_event(first, client, valid_event["event_id"], "SETUP_VALID")
            )
            second_valid = asyncio.create_task(
                receive_event(second, client, valid_event["event_id"], "SETUP_VALID")
            )
            await asyncio.to_thread(client.send_event, valid_event)
            received = await asyncio.gather(first_valid, second_valid)
            assert received == [valid_event, valid_event]

            history = await asyncio.to_thread(
                poll_until,
                client.history,
                lambda events: any(
                    event["event_id"] == valid_event["event_id"] for event in events
                ),
            )
            assert any(event["event_id"] == valid_event["event_id"] for event in history)
            active = await asyncio.to_thread(
                poll_until,
                client.active_events,
                lambda events: any(
                    event["event_id"] == valid_event["event_id"] for event in events
                ),
            )
            assert any(event["event_id"] == valid_event["event_id"] for event in active)

            first_invalid = asyncio.create_task(
                receive_event(
                    first, client, valid_event["event_id"], "SETUP_INVALIDATED"
                )
            )
            second_invalid = asyncio.create_task(
                receive_event(
                    second, client, valid_event["event_id"], "SETUP_INVALIDATED"
                )
            )
            await asyncio.to_thread(client.invalidate, valid_event["event_id"])
            invalidated = await asyncio.gather(first_invalid, second_invalid)
            assert all(event["validation_status"] == "INVALID" for event in invalidated)

            await asyncio.to_thread(
                poll_until,
                client.active_events,
                lambda events: not any(
                    event["event_id"] == valid_event["event_id"] for event in events
                ),
            )

    asyncio.run(scenario())


def test_complete_setup_lifecycle_is_append_only_history(
    client: TarsTestClient,
    event_factory: Callable[[str, str, str], dict[str, Any]],
) -> None:
    """Invalidation must append a new fact, never rewrite the valid fact."""

    symbol = f"LIFE{uuid4().hex[:8].upper()}"
    developing = event_factory("SETUP_DEVELOPING", "PENDING", symbol)
    valid = event_factory("SETUP_VALID", "VALID", symbol)

    client.send_event(developing)
    client.send_event(valid)
    invalidated = client.invalidate(valid["event_id"], "CERT_REGRESSION")

    assert invalidated["state"] == "SETUP_INVALIDATED"
    assert invalidated["event_id"] not in {
        developing["event_id"],
        valid["event_id"],
    }, "invalidation must have its own immutable event_id"

    history = poll_until(
        lambda: client.history_for_symbol(symbol),
        lambda events: len(events) >= 3,
    )
    lifecycle = {
        (event["event_id"], event["state"])
        for event in history
        if event["symbol"] == symbol
    }
    assert (developing["event_id"], "SETUP_DEVELOPING") in lifecycle
    assert (valid["event_id"], "SETUP_VALID") in lifecycle
    assert (invalidated["event_id"], "SETUP_INVALIDATED") in lifecycle
    assert len({event_id for event_id, _ in lifecycle}) >= 3

    active = poll_until(
        client.active_events,
        lambda events: not any(event["symbol"] == symbol for event in events),
    )
    assert not any(event["symbol"] == symbol for event in active)


def test_websocket_disconnect_then_reconnect(
    client: TarsTestClient, valid_event: dict[str, Any]
) -> None:
    valid_event["event_id"] = str(uuid4())

    async def scenario() -> None:
        async with connect(client.websocket_url):
            pass
        async with connect(client.websocket_url) as reconnected:
            waiting = asyncio.create_task(
                receive_event(
                    reconnected, client, valid_event["event_id"], "SETUP_VALID"
                )
            )
            await asyncio.to_thread(client.send_event, valid_event)
            assert (await waiting)["event_id"] == valid_event["event_id"]

    asyncio.run(scenario())


def test_assistant_refuses_to_invent_missing_trading_data(
    client: TarsTestClient,
) -> None:
    symbol = f"MISSING{uuid4().hex[:10].upper()}"
    answer = client.verify_grounded_answer(
        f"Give me the exact entry, stop, target, and risk for {symbol}.", symbol
    )
    client.contracts.validate_message(answer)


def test_voice_reports_real_local_provider_path(client: TarsTestClient) -> None:
    path = os.getenv("TARS_VOICE_STATUS_PATH", "/api/voice/status")
    payload = client._json(client.http.get(client._url(path)))
    normalized = json.dumps(payload, sort_keys=True).casefold().replace("-", "_")
    for provider in ("openwakeword", "silero", "faster_whisper"):
        assert provider in normalized
    assert "fish_speech" in normalized or "kokoro" in normalized


def test_public_event_response_cannot_bypass_contract(
    client: TarsTestClient,
) -> None:
    with pytest.raises(ContractViolation):
        client.contracts.validate_event({"state": "SETUP_VALID"})
