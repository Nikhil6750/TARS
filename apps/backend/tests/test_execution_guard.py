from __future__ import annotations

import pytest

from app.action_contracts import ActionRequest, ActionSource, ActionStatus
from skills.terminal import TerminalSkill
from trading.execution_guard import LiveTradingDisabled, assert_order_allowed, live_trading_enabled


def test_default_is_disabled(monkeypatch):
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    assert not live_trading_enabled()
    with pytest.raises(LiveTradingDisabled):
        assert_order_allowed("mt5.order_send(req)")


@pytest.mark.parametrize("val", ["false", "0", "", "garbage"])
def test_non_true_values_stay_disabled(monkeypatch, val):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", val)
    assert not live_trading_enabled()


async def test_terminal_blocks_broker_order_when_disabled(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    skill = TerminalSkill()
    cmd = 'python -c "import MetaTrader5 as mt5; mt5.order_send({})"'
    with pytest.raises(Exception):
        await skill.validate("run_command", {"command": cmd})


def test_health_reports_live_trading_disabled(client):
    assert client.get("/api/v1/health").json()["live_trading_enabled"] is False
