"""Standalone contract-aware client for a running TARS instance.

This module deliberately imports no code from ``apps/backend`` or ``apps/web``.
It observes the product through public HTTP and WebSocket surfaces only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Self
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator, FormatChecker
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
NUMBER_PATTERN = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")
UNGROUNDED_MARKERS = (
    "no data",
    "no active",
    "not available",
    "unavailable",
    "unknown",
    "cannot determine",
    "can't determine",
    "insufficient data",
)


class ContractViolation(ValueError):
    """Raised when public data does not satisfy a canonical schema."""


class GroundingViolation(AssertionError):
    """Raised when an assistant answer claims facts absent from event state."""


@dataclass(frozen=True)
class Routes:
    health: str = "/health"
    events: str = "/api/events"
    active: str = "/api/events/active"
    history: str = "/api/events/history"
    invalidate: str = "/api/events/{event_id}/invalidate"
    assistant: str = "/api/assistant/messages"
    websocket: str = "/ws"

    @classmethod
    def from_env(cls) -> Routes:
        return cls(
            health=os.getenv("TARS_HEALTH_PATH", cls.health),
            events=os.getenv("TARS_EVENTS_PATH", cls.events),
            active=os.getenv("TARS_ACTIVE_PATH", cls.active),
            history=os.getenv("TARS_HISTORY_PATH", cls.history),
            invalidate=os.getenv("TARS_INVALIDATE_PATH", cls.invalidate),
            assistant=os.getenv("TARS_ASSISTANT_PATH", cls.assistant),
            websocket=os.getenv("TARS_WEBSOCKET_PATH", cls.websocket),
        )


class ContractValidator:
    def __init__(self) -> None:
        checker = FormatChecker()
        self.trading_event = Draft202012Validator(
            self._load("trading-event.schema.json"), format_checker=checker
        )
        self.assistant_message = Draft202012Validator(
            self._load("assistant-message.schema.json"), format_checker=checker
        )

    @staticmethod
    def _load(filename: str) -> dict[str, Any]:
        return json.loads((CONTRACTS / filename).read_text(encoding="utf-8"))

    @staticmethod
    def _validate(validator: Draft202012Validator, value: Any, label: str) -> None:
        errors = sorted(validator.iter_errors(value), key=lambda error: list(error.path))
        if errors:
            detail = "; ".join(
                f"{'/'.join(map(str, error.path)) or '<root>'}: {error.message}"
                for error in errors
            )
            raise ContractViolation(f"Invalid {label}: {detail}")

    def validate_event(self, value: Any) -> dict[str, Any]:
        self._validate(self.trading_event, value, "trading event")
        return value

    def validate_message(self, value: Any) -> dict[str, Any]:
        self._validate(self.assistant_message, value, "assistant message")
        return value


class TarsTestClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        *,
        routes: Routes | None = None,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.base_url = base_url.rstrip("/") + "/"
        self.routes = routes or Routes.from_env()
        self.timeout_seconds = timeout_seconds
        self.contracts = ContractValidator()
        self.http = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds), transport=transport
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.http.close()

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    @property
    def websocket_url(self) -> str:
        split = urlsplit(self._url(self.routes.websocket))
        scheme = "wss" if split.scheme == "https" else "ws"
        return urlunsplit((scheme, split.netloc, split.path, split.query, ""))

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        response.raise_for_status()
        try:
            return response.json()
        except ValueError as exc:
            raise ContractViolation("Public endpoint returned non-JSON data") from exc

    def health(self) -> dict[str, Any]:
        payload = self._json(self.http.get(self._url(self.routes.health)))
        if not isinstance(payload, dict):
            raise ContractViolation("Health response must be a JSON object")
        return payload

    def send_event(self, event: dict[str, Any], *, validate: bool = True) -> Any:
        if validate:
            self.contracts.validate_event(event)
        return self._json(self.http.post(self._url(self.routes.events), json=event))

    def _event_list(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            candidates = payload
        elif isinstance(payload, dict):
            candidates = next(
                (
                    payload[key]
                    for key in ("events", "active_events", "history", "items")
                    if key in payload
                ),
                None,
            )
            if candidates is None and "event" in payload:
                candidates = [payload["event"]]
        else:
            candidates = None
        if not isinstance(candidates, list):
            raise ContractViolation("Event query response has no event list")
        return [self.contracts.validate_event(event) for event in candidates]

    def active_events(self) -> list[dict[str, Any]]:
        return self._event_list(self._json(self.http.get(self._url(self.routes.active))))

    def history(self) -> list[dict[str, Any]]:
        return self._event_list(self._json(self.http.get(self._url(self.routes.history))))

    def history_for_symbol(self, symbol: str) -> list[dict[str, Any]]:
        """Query public history with an explicit symbol filter."""

        payload = self._json(
            self.http.get(self._url(self.routes.history), params={"symbol": symbol})
        )
        return self._event_list(payload)

    def invalidate(self, event_id: str, reason: str = "MANUAL_INVALIDATION") -> Any:
        path = self.routes.invalidate.format(event_id=event_id)
        return self._json(
            self.http.post(
                self._url(path),
                json={"event_id": event_id, "reason_codes": [reason]},
            )
        )

    @staticmethod
    def user_message(content: str, conversation_id: str | None = None) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "message_id": str(uuid4()),
            "conversation_id": conversation_id or str(uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "role": "user",
            "content": content,
            "input_mode": "text",
            "audio_ref": None,
            "related_event_id": None,
            "intent": None,
            "providers": {"stt": None, "assistant": None, "tts": None},
            "error": None,
        }

    @staticmethod
    def _assistant_message(payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict) and isinstance(payload.get("message"), dict):
            return payload["message"]
        if not isinstance(payload, dict):
            raise ContractViolation("Assistant response must be a JSON object")
        return payload

    def ask(self, content: str, conversation_id: str | None = None) -> dict[str, Any]:
        request = self.user_message(content, conversation_id)
        self.contracts.validate_message(request)
        payload = self._json(
            self.http.post(self._url(self.routes.assistant), json=request)
        )
        message = self._assistant_message(payload)
        self.contracts.validate_message(message)
        if message["role"] != "assistant":
            raise ContractViolation("Assistant endpoint did not return role='assistant'")
        return message

    async def websocket_messages(
        self, count: int = 1, timeout_seconds: float | None = None
    ) -> list[dict[str, Any]]:
        if count < 1:
            raise ValueError("count must be at least one")
        timeout = timeout_seconds or self.timeout_seconds
        messages: list[dict[str, Any]] = []
        async with asyncio.timeout(timeout):
            async with connect(
                self.websocket_url,
                open_timeout=timeout,
                close_timeout=min(timeout, 2.0),
            ) as websocket:
                while len(messages) < count:
                    raw = await websocket.recv()
                    payload = json.loads(raw)
                    event = self._unwrap_websocket_event(payload)
                    messages.append(self.contracts.validate_event(event))
        return messages

    @staticmethod
    def _unwrap_websocket_event(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ContractViolation("WebSocket message must be a JSON object")
        for key in ("event", "payload", "data"):
            if isinstance(payload.get(key), dict):
                return payload[key]
        return payload

    def verify_grounded_answer(self, question: str, symbol: str) -> dict[str, Any]:
        known_events = [
            event
            for event in self.active_events() + self.history()
            if event["symbol"].casefold() == symbol.casefold()
        ]
        answer = self.ask(question)
        content = answer["content"]
        related_id = answer.get("related_event_id")
        known_ids = {event["event_id"] for event in known_events}
        if related_id is not None and related_id not in known_ids:
            raise GroundingViolation(
                f"Assistant cited unknown event_id {related_id!r} for {symbol}"
            )

        claimed_numbers = {float(value) for value in NUMBER_PATTERN.findall(content)}
        known_numbers = {
            float(event[field])
            for event in known_events
            for field in (
                "entry",
                "stop_loss",
                "take_profit",
                "risk_reward",
                "risk_percent",
            )
            if event.get(field) is not None
        }
        unknown_numbers = claimed_numbers - known_numbers
        if unknown_numbers:
            raise GroundingViolation(
                f"Assistant claimed numeric trading data absent from state: {sorted(unknown_numbers)}"
            )
        if not known_events and not any(
            marker in content.casefold() for marker in UNGROUNDED_MARKERS
        ):
            raise GroundingViolation(
                "Assistant did not disclose that requested trading data is unavailable"
            )
        return answer


def _load_json(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url", default=os.getenv("TARS_BASE_URL", "http://127.0.0.1:8000")
    )
    parser.add_argument(
        "--timeout", type=float, default=float(os.getenv("TARS_TEST_TIMEOUT", "5"))
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("health")
    send = commands.add_parser("send-event")
    send.add_argument("fixture")
    send.add_argument("--allow-invalid", action="store_true")
    commands.add_parser("active")
    commands.add_parser("history")
    listen = commands.add_parser("listen")
    listen.add_argument("--count", type=int, default=1)
    invalidate = commands.add_parser("invalidate")
    invalidate.add_argument("event_id")
    invalidate.add_argument("--reason", default="MANUAL_INVALIDATION")
    ask = commands.add_parser("ask")
    ask.add_argument("question")
    grounded = commands.add_parser("verify-grounded")
    grounded.add_argument("question")
    grounded.add_argument("--symbol", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        with TarsTestClient(args.base_url, timeout_seconds=args.timeout) as client:
            if args.command == "health":
                result = client.health()
            elif args.command == "send-event":
                result = client.send_event(
                    _load_json(args.fixture), validate=not args.allow_invalid
                )
            elif args.command == "active":
                result = client.active_events()
            elif args.command == "history":
                result = client.history()
            elif args.command == "listen":
                result = asyncio.run(
                    client.websocket_messages(args.count, args.timeout)
                )
            elif args.command == "invalidate":
                result = client.invalidate(args.event_id, args.reason)
            elif args.command == "ask":
                result = client.ask(args.question)
            else:
                result = client.verify_grounded_answer(args.question, args.symbol)
        _print_json(result)
    except (ContractViolation, GroundingViolation, httpx.HTTPError, TimeoutError) as exc:
        print(f"TARS verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
