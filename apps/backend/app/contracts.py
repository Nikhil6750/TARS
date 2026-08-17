"""Loads the frozen, shared JSON Schemas from `contracts/` and validates
against them directly — the canonical source of truth per AGENTS.md (backend
must never fork its own copy of a schema `contracts/` already owns).
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

import jsonschema
from jsonschema import FormatChecker

from app.config import REPO_ROOT

CONTRACTS_DIR = REPO_ROOT / "contracts"


@lru_cache
def _load_schema(filename: str) -> dict[str, Any]:
    path = CONTRACTS_DIR / filename
    return json.loads(path.read_text(encoding="utf-8"))


def trading_event_schema() -> dict[str, Any]:
    return _load_schema("trading-event.schema.json")


def assistant_message_schema() -> dict[str, Any]:
    return _load_schema("assistant-message.schema.json")


def action_request_schema() -> dict[str, Any]:
    return _load_schema("action-request.schema.json")


def action_result_schema() -> dict[str, Any]:
    return _load_schema("action-result.schema.json")


class ContractValidationError(ValueError):
    """Raised when a payload does not conform to a frozen contracts/*.schema.json."""


def validate_trading_event(payload: dict[str, Any]) -> None:
    try:
        jsonschema.validate(
            payload, trading_event_schema(), format_checker=FormatChecker()
        )
    except jsonschema.ValidationError as exc:
        raise ContractValidationError(str(exc.message)) from exc


def validate_assistant_message(payload: dict[str, Any]) -> None:
    try:
        jsonschema.validate(
            payload, assistant_message_schema(), format_checker=FormatChecker()
        )
    except jsonschema.ValidationError as exc:
        raise ContractValidationError(str(exc.message)) from exc


def validate_action_request(payload: dict[str, Any]) -> None:
    try:
        jsonschema.validate(
            payload, action_request_schema(), format_checker=FormatChecker()
        )
    except jsonschema.ValidationError as exc:
        raise ContractValidationError(str(exc.message)) from exc


def validate_action_result(payload: dict[str, Any]) -> None:
    try:
        jsonschema.validate(
            payload, action_result_schema(), format_checker=FormatChecker()
        )
    except jsonschema.ValidationError as exc:
        raise ContractValidationError(str(exc.message)) from exc
