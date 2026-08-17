from __future__ import annotations

from uuid import UUID

from agent_runtime.contracts import (
    StrategyAvailability,
    StrategyContext,
    StrategyDefinition,
    StrategyProvider,
    StrategySignal,
)


class NotConfiguredStrategyProvider:
    name = "not_configured"

    async def get_definition(self, strategy_id: str) -> StrategyDefinition:
        raise RuntimeError("StrategyProvider is not configured")

    async def get_signals(
        self, definition: StrategyDefinition, *, job_id: UUID
    ) -> tuple[StrategySignal, ...]:
        raise RuntimeError("StrategyProvider is not configured")


class QuantBrainBoundary:
    """Read-only boundary; TARS cannot validate, mutate, or invent strategy facts."""

    def __init__(self, provider: StrategyProvider | None = None) -> None:
        self.provider = provider

    async def context(self, strategy_id: str | None, *, job_id: UUID) -> StrategyContext:
        if strategy_id is None or self.provider is None or isinstance(
            self.provider, NotConfiguredStrategyProvider
        ):
            return StrategyContext(availability=StrategyAvailability.NOT_CONFIGURED)
        try:
            definition = await self.provider.get_definition(strategy_id)
            if definition.strategy_id != strategy_id:
                raise ValueError("strategy definition id mismatch")
            signals = await self.provider.get_signals(definition, job_id=job_id)
            if any(signal.strategy_id != strategy_id for signal in signals):
                raise ValueError("strategy signal id mismatch")
            return StrategyContext(
                availability=StrategyAvailability.AVAILABLE,
                definition=definition,
                signals=signals,
            )
        except Exception as exc:
            return StrategyContext(
                availability=StrategyAvailability.PROVIDER_FAILED,
                error=f"{type(exc).__name__}: strategy provider failed",
            )
