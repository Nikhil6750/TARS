"""Transaction-cost model. Fills use real bid/ask (long buys ask, sells bid; short the reverse). No mid fills.
All values are in PRICE units so the model is instrument-agnostic; pass spread_mult to stress it."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CostModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    spread_mult: float = Field(1.0, ge=0.0, description="multiplier on observed bid/ask spread (>=1 stresses)")
    commission_round_turn: float = Field(0.0, ge=0.0, description="price units per round turn")
    slippage_per_side: float = Field(0.0, ge=0.0, description="adverse price units on each entry and exit")
    latency_bars: int = Field(0, ge=0, le=10, description="extra bars between signal and fill")
    financing_per_bar: float = Field(0.0, ge=0.0, description="PLACEHOLDER swap/financing cost per held bar (price units)")
    financing_is_placeholder: bool = True

    def with_stress(self, spread_mult: float | None = None, slippage_per_side: float | None = None) -> "CostModel":
        return self.model_copy(update={
            "spread_mult": self.spread_mult if spread_mult is None else spread_mult,
            "slippage_per_side": self.slippage_per_side if slippage_per_side is None else slippage_per_side})

    def spread_adjustment(self, observed_spread: float) -> float:
        """Extra price paid on one side when spread_mult > 1 (half of the added spread per side)."""
        return max(self.spread_mult - 1.0, 0.0) * observed_spread / 2.0

    def entry_fill(self, direction: int, bid: float, ask: float) -> float:
        half_extra = self.spread_adjustment(ask - bid)
        return (ask + half_extra + self.slippage_per_side) if direction > 0 else (bid - half_extra - self.slippage_per_side)

    def exit_fill(self, direction: int, bid: float, ask: float) -> float:
        half_extra = self.spread_adjustment(ask - bid)
        return (bid - half_extra - self.slippage_per_side) if direction > 0 else (ask + half_extra + self.slippage_per_side)

    def holding_cost(self, bars_held: int) -> float:
        return self.financing_per_bar * bars_held + self.commission_round_turn

    def adjust_exit(self, direction: int, level: float, spread: float) -> float:
        """Apply stress spread + slippage to an exit achieved at a bid/ask-side `level`."""
        adj = self.spread_adjustment(spread) + self.slippage_per_side
        return level - adj if direction > 0 else level + adj
