"""TarsOrchestrator — the single entry point above `AssistantRouter`, per
the target architecture:

    Voice/Text/Event -> TARS Orchestrator -> Intelligence/deterministic
    routing -> Skills or ActionPlan -> ActionRuntime -> PermissionEngine
    -> Skills -> Result -> Memory/Audit -> TARS streamed text + voice

`AssistantRouter` already implements the innermost "deterministic vs
conversation" split (see `assistant/router.py`) and conversation-turn
persistence; this module wraps it rather than duplicating it, and adds the
layer above: explicit-memory commands, trading-skill dispatch through the
authoritative `ActionRuntime` (so trading intents get the exact same
permission/audit path as any HUD/voice-triggered action -- no bypass), and
optional on-demand agent triggering. Anything not recognized here still
falls through to `AssistantRouter.handle_text`, which keeps its own
deterministic patterns and LLM fallback exactly as before -- this module
only ever adds routing in front of it, never replaces it.

Every non-conversational route this module handles (skill dispatch, agent
trigger) is recorded as a decision in memory (`MemoryService.save_decision`)
-- the "task/decision memory" the orchestrator owns, distinct from
`AssistantRouter`'s plain conversation-turn persistence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

from actions.runtime import ActionRuntime
from app.action_contracts import ActionRequest, ActionSource, ActionStatus
from app.latency import LatencyTracker
from app.schemas import AssistantMessage, InputMode, MessageProviders, MessageRole
from assistant.conversation_store import ConversationStore
from assistant.router import AssistantRouter, RouterReply
from memory.service import MemoryService
from orchestrator import patterns


@runtime_checkable
class RunnableAgent(Protocol):
    name: str

    async def run_once(self) -> Any: ...


class OrchestratorAgentRuntime(Protocol):
    async def run_on_demand(self, agent: RunnableAgent, *, trigger: str = "orchestrator") -> Any: ...


@dataclass
class _Route:
    kind: str
    payload: dict[str, Any]


class TarsOrchestrator:
    def __init__(
        self,
        *,
        assistant_router: AssistantRouter,
        action_runtime: ActionRuntime,
        memory_service: MemoryService,
        conversation_store: ConversationStore,
        agent_runtime: OrchestratorAgentRuntime | None = None,
        agents: dict[str, RunnableAgent] | None = None,
    ) -> None:
        self._assistant_router = assistant_router
        self._action_runtime = action_runtime
        self._memory = memory_service
        self._conversations = conversation_store
        self._agent_runtime = agent_runtime
        self._agents = agents or {}

    async def handle_text(self, text: str, conversation_id: str | None) -> RouterReply:
        conversation_id = conversation_id or str(uuid4())
        tracker = LatencyTracker()
        tracker.mark("reasoning_start")
        route = self._classify(text)
        if route is None:
            # Falls through to AssistantRouter, which may itself bypass the
            # LLM (its own deterministic patterns) or call the configured
            # AssistantProvider -- either way this is the "reasoning" span
            # the TARS core § Performance latency measurement cares about.
            reply = await self._assistant_router.handle_text(text, conversation_id)
            tracker.mark("reasoning_end")
            tracker.emit_span("latency.orchestrator_turn")
            return reply

        user_message = AssistantMessage(
            conversation_id=UUID(conversation_id),
            role=MessageRole.user,
            content=text,
            input_mode=InputMode.text,
        )
        await self._save(user_message)

        handler = getattr(self, f"_handle_{route.kind}")
        reply_text, intent = await handler(route.payload, conversation_id)
        tracker.mark("reasoning_end")
        tracker.emit_span("latency.orchestrator_turn")
        assistant_message = AssistantMessage(
            conversation_id=UUID(conversation_id),
            role=MessageRole.assistant,
            content=reply_text,
            input_mode=InputMode.text,
            intent=intent,
            providers=MessageProviders(assistant="deterministic"),
        )
        await self._save(assistant_message)
        return RouterReply(
            conversation_id=conversation_id,
            user_message=user_message,
            assistant_message=assistant_message,
        )

    async def handle_text_stream(self, text: str, conversation_id: str | None):
        """Streaming twin of handle_text -- see AssistantRouter.handle_text_stream
        for the event shape (`delta` / `complete`). Deterministic
        orchestrator routes (skill dispatch, memory commands) are already
        fast/non-LLM, so they're delivered as a single delta + complete
        pair rather than a real token stream; only the LLM fallthrough to
        AssistantRouter actually streams incrementally."""
        conversation_id = conversation_id or str(uuid4())
        tracker = LatencyTracker()
        tracker.mark("reasoning_start")
        route = self._classify(text)
        if route is None:
            async for event in self._assistant_router.handle_text_stream(text, conversation_id):
                yield event
            tracker.mark("reasoning_end")
            tracker.emit_span("latency.orchestrator_turn")
            return

        user_message = AssistantMessage(
            conversation_id=UUID(conversation_id),
            role=MessageRole.user,
            content=text,
            input_mode=InputMode.text,
        )
        await self._save(user_message)

        handler = getattr(self, f"_handle_{route.kind}")
        reply_text, intent = await handler(route.payload, conversation_id)
        tracker.mark("reasoning_end")
        tracker.emit_span("latency.orchestrator_turn")
        assistant_message = AssistantMessage(
            conversation_id=UUID(conversation_id),
            role=MessageRole.assistant,
            content=reply_text,
            input_mode=InputMode.text,
            intent=intent,
            providers=MessageProviders(assistant="deterministic"),
        )
        await self._save(assistant_message)
        yield {"type": "delta", "text": reply_text}
        yield {"type": "complete", "message": assistant_message.to_contract_dict()}

    # ---- routing ----------------------------------------------------------

    def _classify(self, text: str) -> _Route | None:
        if match := patterns.REMEMBER.search(text):
            return _Route("remember", {"text": match.group(1).strip()})
        if match := patterns.SAVE_TRADING_OBSERVATION.search(text):
            return _Route("save_trading_observation", {"text": match.group(1).strip()})
        if match := patterns.SEARCH_TRADING_MEMORY.search(text):
            query = (match.group(1) or match.group(2) or "").strip()
            return _Route("search_trading_memory", {"query": query})
        if match := patterns.EXPLAIN_SETUP.search(text):
            return _Route("explain_setup", {"symbol": match.group(1).strip()})
        if patterns.TRADING_CONTEXT.search(text):
            return _Route("trading_context", {})
        if patterns.ANALYZE_CHART.search(text):
            return _Route("analyze_active_chart", {})
        if patterns.SETUP_TRADING_WORKSPACE.search(text):
            return _Route("trading_workspace", {})
        if patterns.FOCUS_TRADINGVIEW.search(text):
            return _Route("focus_tradingview", {})
        if patterns.OPEN_TRADINGVIEW.search(text):
            return _Route("open_tradingview", {})
        return None

    # ---- handlers -----------------------------------------------------------

    async def _handle_remember(
        self, payload: dict[str, Any], conversation_id: str
    ) -> tuple[str, str]:
        text = payload["text"]
        note_id = await self._memory.remember(text, actor="user", conversation_id=conversation_id)
        await self._record_decision(
            f"Remembered a fact at the user's explicit request (note {note_id}).",
            conversation_id,
        )
        return f"Got it, I'll remember that: {text}", "remember"

    async def _handle_save_trading_observation(
        self, payload: dict[str, Any], conversation_id: str
    ) -> tuple[str, str]:
        result = await self._dispatch_trading(
            "save_trading_observation", {"text": payload["text"]}, conversation_id
        )
        if result.status != ActionStatus.SUCCEEDED:
            return (
                f"I couldn't save that trading observation: {result.error or result.summary}",
                "save_trading_observation",
            )
        return "Saved that trading observation.", "save_trading_observation"

    async def _handle_search_trading_memory(
        self, payload: dict[str, Any], conversation_id: str
    ) -> tuple[str, str]:
        query = payload["query"]
        result = await self._dispatch_trading(
            "search_trading_memory", {"query": query}, conversation_id
        )
        if result.status != ActionStatus.SUCCEEDED:
            return f"I couldn't search trading memory: {result.error}", "search_trading_memory"
        results = result.data.get("results", [])
        if not results:
            return f"No trading observations found for '{query}'.", "search_trading_memory"
        lines = [f"Found {len(results)} trading observation(s) for '{query}':"]
        lines += [f"- {r['snippet']}" for r in results]
        return "\n".join(lines), "search_trading_memory"

    async def _handle_explain_setup(
        self, payload: dict[str, Any], conversation_id: str
    ) -> tuple[str, str]:
        result = await self._dispatch_trading(
            "explain_setup", {"symbol": payload["symbol"]}, conversation_id
        )
        if result.status != ActionStatus.SUCCEEDED:
            return f"I couldn't explain that setup: {result.error}", "explain_setup"
        return result.summary, "explain_setup"

    async def _handle_trading_context(
        self, payload: dict[str, Any], conversation_id: str
    ) -> tuple[str, str]:
        result = await self._dispatch_trading("get_trading_context", {}, conversation_id)
        if result.status != ActionStatus.SUCCEEDED:
            return f"I couldn't retrieve trading context: {result.error}", "trading_context"
        return result.summary, "trading_context"

    async def _handle_analyze_active_chart(
        self, payload: dict[str, Any], conversation_id: str
    ) -> tuple[str, str]:
        result = await self._dispatch_trading("analyze_active_chart", {}, conversation_id)
        if result.status != ActionStatus.SUCCEEDED:
            return f"I couldn't analyze the active chart: {result.error}", "analyze_active_chart"
        await self._record_decision(
            "Analyzed the active chart on the user's request and saved the read.",
            conversation_id,
        )
        return result.summary, "analyze_active_chart"

    async def _handle_open_tradingview(
        self, payload: dict[str, Any], conversation_id: str
    ) -> tuple[str, str]:
        result = await self._dispatch_trading("open_tradingview", {}, conversation_id)
        if result.status != ActionStatus.SUCCEEDED:
            return f"I couldn't open TradingView: {result.error}", "open_tradingview"
        return result.summary, "open_tradingview"

    async def _handle_focus_tradingview(
        self, payload: dict[str, Any], conversation_id: str
    ) -> tuple[str, str]:
        result = await self._dispatch_trading("focus_tradingview", {}, conversation_id)
        if result.status != ActionStatus.SUCCEEDED:
            return f"I couldn't focus TradingView: {result.error}", "focus_tradingview"
        return result.summary, "focus_tradingview"

    async def _handle_trading_workspace(
        self, payload: dict[str, Any], conversation_id: str
    ) -> tuple[str, str]:
        agent = self._agents.get("trading_workspace_agent")
        if agent is None or self._agent_runtime is None:
            # Fall back to a direct skill call so the request still does
            # something useful even before the agent framework is wired in.
            return await self._handle_focus_tradingview(payload, conversation_id)
        run_result = await self._agent_runtime.run_on_demand(agent, trigger="orchestrator")
        await self._record_decision(
            f"Triggered trading_workspace_agent on the user's request: {run_result.summary}",
            conversation_id,
        )
        return run_result.summary, "trading_workspace"

    # ---- shared helpers -------------------------------------------------

    async def _dispatch_trading(
        self, action: str, arguments: dict[str, Any], conversation_id: str
    ):
        request = ActionRequest(
            skill="trading",
            action=action,
            arguments=arguments,
            source=ActionSource.deterministic,
        )
        result = await self._action_runtime.submit(request)
        await self._record_decision(
            f"Dispatched trading.{action}() via the orchestrator: {result.status.value}.",
            conversation_id,
        )
        return result

    async def _record_decision(self, text: str, conversation_id: str) -> None:
        try:
            await self._memory.save_decision(text, actor="orchestrator", conversation_id=conversation_id)
        except Exception:
            # Decision memory is best-effort context, never a gate on the
            # user-facing reply already produced.
            pass

    async def _save(self, message: AssistantMessage) -> None:
        await self._conversations.save(message)
        await self._memory.index_conversation_message(message)
