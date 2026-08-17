"""StrategyProvider — the single interface trading skills/agents use to ask
"is a strategy configured, and if so what is it" without ever guessing.

`NullStrategyProvider` is the only implementation shipped in this stream: it
always reports `StrategyStatus.NOT_CONFIGURED`. This is intentional, not a
stub to silently replace later — MASTER_SPEC.md is explicit that TARS must
never invent strategy rules, and `quant_brain` (not this codebase) remains
the authority for validated strategy claims (ARCHITECTURE.md § quant_brain
boundary). A future `QuantBrainStrategyProvider` would implement this same
interface against `settings.quant_brain_base_url` once that integration
exists; nothing in the trading skills/agents needs to change when it does.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from trading.models import StrategyDefinition, StrategyStatus


class StrategyProvider(ABC):
    @abstractmethod
    async def status(self) -> StrategyStatus:
        """Never raises for "not configured" — that is a normal, honest
        return value, not an error condition."""

    @abstractmethod
    async def get_strategy(self, strategy_id: str | None = None) -> StrategyDefinition | None:
        """Returns None if no strategy is configured, or the named
        strategy_id isn't known. Never fabricates a StrategyDefinition."""


class NullStrategyProvider(StrategyProvider):
    """The default, honest provider: no strategy source is wired in."""

    async def status(self) -> StrategyStatus:
        return StrategyStatus.NOT_CONFIGURED

    async def get_strategy(self, strategy_id: str | None = None) -> StrategyDefinition | None:
        return None


def build_strategy_provider(quant_brain_base_url: str | None) -> StrategyProvider:
    """Choke point for future strategy-source wiring — mirrors
    assistant/factory.py's pattern. `quant_brain_base_url` is accepted (not
    just ignored) so the call site reads honestly, but a real
    QuantBrainStrategyProvider is out of scope for this stream (quant_brain
    integration is explicitly deferred, per MASTER_SPEC.md); wiring one in
    later is a one-line change here, same as swapping an AssistantProvider."""
    return NullStrategyProvider()
