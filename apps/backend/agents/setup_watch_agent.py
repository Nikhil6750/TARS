"""SetupWatchAgent — a CONTINUOUS agent that periodically diffs
`TradingContext.active_setups` against the last-seen snapshot and records a
`decision` memory note for anything new or changed, so "what changed while
I was away" has a durable trail without a human polling manually. Per
ARCHITECTURE.md § Scheduling, this is a housekeeping/recall aid layered on
top of the already-event-driven trading-event stream -- it does not detect
setups itself, `EventService`/the mock generator (or later quant_brain) do
that; this agent only notices and records that `TradingContextBuilder`'s
view of `active_setups` changed.

CRITICAL CONSTRAINT (MASTER_SPEC.md / ARCHITECTURE.md § quant_brain
boundary, mirrored in trading/models.py's `TradingContext.as_grounding_text`
and skills/trading.py's `_explain_setup_deterministic`): TARS must never
fabricate a strategy verdict, a confidence score, or a trade signal. While
`TradingContext.strategy_status` is `NOT_CONFIGURED` (the default -- see
`trading/provider.py`'s `NullStrategyProvider`), the decision text this
agent saves is built from *only* the deterministic raw fields already
present on the setup dict (symbol, state, direction, entry, stop_loss,
take_profit, risk_reward, validation_status, reason_codes) -- no
interpretive language layered on top. No "recommend", no confidence
wording, no "you should buy/sell". If a real StrategyProvider is wired in
later and strategy_status becomes CONFIGURED, this still does not become a
place to invent a verdict -- it only ever restates deterministic
TradingEvent fields verbatim, exactly like `_explain_setup_deterministic`
already does for the `explain_setup` skill action.
"""
from __future__ import annotations

from typing import Any

from agents.base import Agent
from agents.models import AgentConfig, AgentMode, AgentRunResult, AgentRunStatus
from memory.service import MemoryService
from trading.context import TradingContextBuilder
from trading.models import StrategyStatus

_DEFAULT_CONFIG = AgentConfig(mode=AgentMode.CONTINUOUS, interval_seconds=30.0, timeout_seconds=15.0)

# A setup counts as "changed" if either of these deterministic fields moved
# since the last iteration -- entry/stop/take-profit can be re-published on
# the same state without being a meaningful change worth a new decision
# note; state/validation_status transitions are what actually matter here.
_DIFF_FIELDS = ("state", "validation_status")


class SetupWatchAgent(Agent):
    name = "setup_watch_agent"

    def __init__(
        self,
        context_builder: TradingContextBuilder,
        memory_service: MemoryService,
        *,
        config: AgentConfig | None = None,
    ) -> None:
        super().__init__(config or _DEFAULT_CONFIG)
        self._context_builder = context_builder
        self._memory = memory_service
        # Instance state only -- a live-state watcher, not a durable store.
        # Reset on process restart is fine: the next iteration re-diffs
        # against whatever TradingContext currently reports as active, so a
        # restart just means "everything currently active looks new once".
        self._last_seen: dict[str, dict[str, Any]] = {}

    async def run_once(self) -> AgentRunResult:
        context = await self._context_builder.build()

        changed: list[dict[str, Any]] = []
        current_symbols: set[str] = set()
        for setup in context.active_setups:
            symbol = setup.get("symbol")
            if not symbol:
                continue
            current_symbols.add(symbol)
            previous = self._last_seen.get(symbol)
            if previous is None or any(
                previous.get(field_name) != setup.get(field_name) for field_name in _DIFF_FIELDS
            ):
                changed.append(setup)
            self._last_seen[symbol] = setup

        # Drop symbols that are no longer active so a later reappearance is
        # treated as new rather than silently deduped against stale state.
        for symbol in list(self._last_seen):
            if symbol not in current_symbols:
                self._last_seen.pop(symbol, None)

        for setup in changed:
            await self._memory.save_decision(
                _describe_change(setup, context.strategy_status),
                actor="agent:setup_watch_agent",
                tags=["setup_watch_agent", str(setup.get("symbol") or "")],
                metadata={"symbol": setup.get("symbol"), "state": setup.get("state")},
            )

        # Zero changes is a normal, successful iteration -- not a failure.
        return AgentRunResult(
            status=AgentRunStatus.SUCCEEDED,
            summary=f"{len(changed)} setup change(s) observed",
            data={
                "changed_symbols": [s.get("symbol") for s in changed],
                "strategy_status": context.strategy_status.value,
            },
        )


def _describe_change(setup: dict[str, Any], strategy_status: StrategyStatus) -> str:
    """Builds decision text from raw TradingEvent fields only -- see this
    module's CRITICAL CONSTRAINT docstring above. Every clause here is a
    direct restatement of a field already on `setup`; none of it is
    inference, evaluation, or a recommendation."""
    parts = [f"{setup.get('symbol')} changed to {setup.get('state')}"]
    if setup.get("direction"):
        parts.append(f"direction {setup['direction']}")
    if setup.get("entry") is not None:
        parts.append(f"entry {setup['entry']}")
    if setup.get("stop_loss") is not None:
        parts.append(f"stop loss {setup['stop_loss']}")
    if setup.get("take_profit") is not None:
        parts.append(f"take profit {setup['take_profit']}")
    if setup.get("risk_reward") is not None:
        parts.append(f"R:R {setup['risk_reward']}")
    if setup.get("validation_status"):
        parts.append(f"validation status {setup['validation_status']}")
    reason_codes = setup.get("reason_codes") or []
    if reason_codes:
        parts.append(f"reason codes: {', '.join(reason_codes)}")
    text = ", ".join(parts) + "."
    if strategy_status == StrategyStatus.NOT_CONFIGURED:
        text += " No strategy configured -- this is raw deterministic state only."
    return text
