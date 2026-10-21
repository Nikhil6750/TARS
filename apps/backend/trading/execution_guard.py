"""Hard execution boundary: nothing may reach a broker unless LIVE_TRADING_ENABLED=true.

TARS has no order-placement code. This guard is the single choke point every surface that
could otherwise reach a broker (terminal commands, scripted MT5 calls, voice terminal tool)
must pass. It reads the environment on every call, defaults to disabled, and is independent of
MT5 connection state.
"""
from __future__ import annotations

import os
import re

BROKER_ORDER_PATTERN = re.compile(
    r"order_send|ordersend|order_check|mt5\.(buy|sell)|positions_close|trade_request", re.IGNORECASE
)
DISABLED_MESSAGE = "Live trading is disabled (LIVE_TRADING_ENABLED=false); no order can reach a broker."


class LiveTradingDisabled(RuntimeError):
    pass


def live_trading_enabled() -> bool:
    return os.environ.get("LIVE_TRADING_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def is_broker_order(text: str) -> bool:
    return bool(BROKER_ORDER_PATTERN.search(text or ""))


def assert_order_allowed(text: str = "") -> None:
    """Raise LiveTradingDisabled for any broker-order attempt while the switch is off."""
    if not live_trading_enabled():
        raise LiveTradingDisabled(DISABLED_MESSAGE)
