from __future__ import annotations

import pytest

from app.contracts import ContractValidationError, validate_trading_event
from app.schemas import EventSource, EventState, TradingEvent, ValidationStatus


def test_minimal_valid_event_passes_contract():
    event = TradingEvent(
        source=EventSource.mock,
        symbol="XAUUSD",
        state=EventState.IDLE,
        validation_status=ValidationStatus.PENDING,
    )
    validate_trading_event(event.to_contract_dict())


def test_full_setup_event_passes_contract():
    event = TradingEvent(
        source=EventSource.mock,
        symbol="ES",
        strategy_id="mock-lifecycle-v1",
        state=EventState.SETUP_VALID,
        direction="LONG",
        entry=5300.0,
        stop_loss=5290.0,
        take_profit=5320.0,
        risk_reward=2.0,
        risk_percent=1.0,
        validation_status=ValidationStatus.VALID,
        reason_codes=["MOCK"],
        warnings=[],
    )
    validate_trading_event(event.to_contract_dict())


def test_unknown_field_is_rejected():
    event = TradingEvent(
        source=EventSource.mock,
        symbol="XAUUSD",
        state=EventState.IDLE,
        validation_status=ValidationStatus.PENDING,
    )
    payload = event.to_contract_dict()
    payload["ai_confidence"] = 0.9
    with pytest.raises(ContractValidationError):
        validate_trading_event(payload)


def test_no_confidence_field_exists_on_model():
    event = TradingEvent(
        source=EventSource.mock,
        symbol="XAUUSD",
        state=EventState.IDLE,
        validation_status=ValidationStatus.PENDING,
    )
    assert "confidence" not in event.model_dump()
