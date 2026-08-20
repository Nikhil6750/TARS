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


# Conservative cap for injected SKILL.md content -- see _handle_skill_use's
# comment. Verified: 8000 raw chars of real markdown already triggered
# "The command line is too long" from the `claude` CLI on this machine.
# Left well below that: AssistantRouter.handle_text() also appends its own
# system_context (active setups + retrieved memory notes, grounding.py) as
# a SEPARATE --append-system-prompt argv element, and Windows enforces a
# limit on the WHOLE command line, not each argument individually -- so
# this cap has to leave headroom for that too, not just for itself.
_SKILL_CONTENT_PROMPT_CAP = 1500


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
        skill_manager: Any | None = None,
        pending_skill_confirmations: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._assistant_router = assistant_router
        self._action_runtime = action_runtime
        self._memory = memory_service
        self._conversations = conversation_store
        self._agent_runtime = agent_runtime
        self._agents = agents or {}
        # skill_manager is used only for read-only metadata lookups the
        # orchestrator needs to compose a reply (e.g. showing full
        # name/description/source/trust before an install confirmation) --
        # every state-changing operation still goes through
        # self._action_runtime.submit()/.confirm(), the same
        # ActionRuntime/PermissionEngine path every other action uses.
        self._skill_manager = skill_manager
        # conversation_id -> {"pending": (request_id, token, identifier, action) | None,
        #                      "last_identifier": str | None}
        # Lives on app.state (see app/main.py) since this orchestrator is
        # constructed fresh per request -- an in-process dict here would be
        # wiped between the "install this?" turn and the "confirm" turn.
        self._skill_state = pending_skill_confirmations if pending_skill_confirmations is not None else {}

    async def handle_text(self, text: str, conversation_id: str | None) -> RouterReply:
        conversation_id = conversation_id or str(uuid4())
        tracker = LatencyTracker()
        tracker.mark("reasoning_start")
        route = self._classify(text, conversation_id)
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
        route = self._classify(text, conversation_id)
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

    def _classify(self, text: str, conversation_id: str) -> _Route | None:
        # Only ever consulted when this exact conversation actually has a
        # pending skill install/uninstall/update confirmation -- otherwise
        # an ordinary "yes"/"cancel" in normal conversation would wrongly
        # get intercepted here.
        state = self._skill_state.get(conversation_id)
        if state and state.get("pending"):
            if patterns.SKILL_CONFIRM.search(text):
                return _Route("skill_confirm", {"approved": True})
            if patterns.SKILL_DENY.search(text):
                return _Route("skill_confirm", {"approved": False})

        if patterns.SKILL_LIST_INSTALLED.search(text):
            return _Route("skill_list_installed", {})
        if patterns.SKILL_UPDATE_ALL.search(text):
            return _Route("skill_update_all", {})
        if match := patterns.SKILL_UPDATE_ONE.search(text):
            target = (match.group(1) or match.group(2) or "").strip()
            return _Route("skill_update_one", {"target": target})
        if match := patterns.SKILL_INSTALL_EXACT.search(text):
            return _Route("skill_install", {"identifier": match.group(1).strip()})
        if match := patterns.SKILL_UNINSTALL.search(text):
            target = (match.group(1) or "").strip()
            return _Route("skill_uninstall", {"target": target})
        if match := patterns.SKILL_USE.search(text):
            if match.group(3):
                return _Route("skill_use", {"target": None, "task": match.group(3).strip()})
            return _Route("skill_use", {"target": (match.group(1) or "").strip(), "task": (match.group(2) or "").strip()})
        if match := patterns.SKILL_SEARCH.search(text):
            query = (match.group(1) or match.group(2) or match.group(3) or "").strip()
            return _Route("skill_search", {"query": query})
        if match := patterns.SKILL_INSTALL_TOPIC.search(text):
            return _Route("skill_install_topic", {"topic": match.group(1).strip()})

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

    # ---- skill registry handlers -----------------------------------------

    async def _handle_skill_list_installed(
        self, payload: dict[str, Any], conversation_id: str
    ) -> tuple[str, str]:
        result = await self._dispatch_skill("list_installed", {}, conversation_id)
        if result.status != ActionStatus.SUCCEEDED:
            return f"I couldn't list installed skills: {result.error}", "skill_list_installed"
        installed = result.data.get("installed", [])
        if not installed:
            return "No skills are installed yet.", "skill_list_installed"
        lines = [f"{len(installed)} installed skill(s):"]
        lines += [f"- {s['name']} (`{s['identifier']}`) — {s['category']}" for s in installed]
        return "\n".join(lines), "skill_list_installed"

    async def _handle_skill_search(
        self, payload: dict[str, Any], conversation_id: str
    ) -> tuple[str, str]:
        query = payload["query"]
        result = await self._dispatch_skill("search_skills", {"query": query, "limit": 5}, conversation_id)
        if result.status != ActionStatus.SUCCEEDED:
            return f"I couldn't search skills: {result.error}", "skill_search"
        results = result.data.get("results", [])
        if not results:
            return f"No skills found for '{query}'.", "skill_search"
        lines = [f"Top skill(s) for '{query}':"]
        lines += [f"- {r['name']} (`{r['identifier']}`) — {r['source']}/{r['trust_level']}: {r['description']}" for r in results]
        return "\n".join(lines), "skill_search"

    async def _handle_skill_install(
        self, payload: dict[str, Any], conversation_id: str
    ) -> tuple[str, str]:
        return await self._begin_skill_install(payload["identifier"], conversation_id)

    async def _handle_skill_install_topic(
        self, payload: dict[str, Any], conversation_id: str
    ) -> tuple[str, str]:
        topic = payload["topic"]
        search = await self._dispatch_skill("search_skills", {"query": topic, "limit": 1}, conversation_id)
        if search.status != ActionStatus.SUCCEEDED or not search.data.get("results"):
            return f"I couldn't find a skill matching '{topic}'.", "skill_install"
        top = search.data["results"][0]
        return await self._begin_skill_install(top["identifier"], conversation_id)

    async def _begin_skill_install(self, identifier: str, conversation_id: str) -> tuple[str, str]:
        inspected = await self._dispatch_skill("inspect_skill", {"identifier": identifier}, conversation_id)
        if inspected.status != ActionStatus.SUCCEEDED:
            return f"I couldn't find skill '{identifier}' in the catalog.", "skill_install"
        record = inspected.data["skill"]

        result = await self._dispatch_skill("install_skill", {"identifier": identifier}, conversation_id)
        if result.status != ActionStatus.CONFIRMATION_REQUIRED:
            return f"Unexpected response requesting install of '{identifier}': {result.status.value}.", "skill_install"

        self._skill_state.setdefault(conversation_id, {})["pending"] = (
            str(result.request_id), result.data["confirmation_token"], identifier, "install_skill",
        )
        platform = record.get("platform") or []
        summary = (
            f"Found a skill to install:\n"
            f"- **Name:** {record['name']}\n"
            f"- **Description:** {record['description']}\n"
            f"- **Source:** {record['source']}\n"
            f"- **Trust level:** {record['trust_level']}\n"
            f"- **Platform:** {', '.join(platform) if platform else 'unspecified'}\n"
            f"- **Identifier:** `{identifier}`\n\n"
            f"Say \"confirm\" to install it, or \"cancel\" to skip."
        )
        return summary, "skill_install"

    async def _handle_skill_uninstall(
        self, payload: dict[str, Any], conversation_id: str
    ) -> tuple[str, str]:
        target = payload["target"]
        identifier = await self._resolve_installed_target(target, conversation_id)
        if identifier is None:
            return f"I couldn't find an installed skill matching '{target}'.", "skill_uninstall"

        result = await self._dispatch_skill("uninstall_skill", {"identifier": identifier}, conversation_id)
        if result.status != ActionStatus.CONFIRMATION_REQUIRED:
            return f"Unexpected response requesting uninstall of '{identifier}': {result.status.value}.", "skill_uninstall"

        self._skill_state.setdefault(conversation_id, {})["pending"] = (
            str(result.request_id), result.data["confirmation_token"], identifier, "uninstall_skill",
        )
        return (
            f"Remove installed skill `{identifier}`? Say \"confirm\" to remove it, or \"cancel\" to keep it.",
            "skill_uninstall",
        )

    async def _handle_skill_update_all(
        self, payload: dict[str, Any], conversation_id: str
    ) -> tuple[str, str]:
        result = await self._dispatch_skill("list_installed", {}, conversation_id)
        installed = result.data.get("installed", []) if result.status == ActionStatus.SUCCEEDED else []
        if not installed:
            return "No skills are installed to update.", "skill_update"
        # Each update is its own CONFIRM_REQUIRED action -- ask about the
        # first one now rather than silently approving a whole batch.
        return await self._begin_skill_install(installed[0]["identifier"], conversation_id)

    async def _handle_skill_update_one(
        self, payload: dict[str, Any], conversation_id: str
    ) -> tuple[str, str]:
        target = payload["target"]
        identifier = await self._resolve_installed_target(target, conversation_id)
        if identifier is None:
            return f"I couldn't find an installed skill matching '{target}'.", "skill_update"
        return await self._begin_skill_install(identifier, conversation_id)

    async def _handle_skill_confirm(
        self, payload: dict[str, Any], conversation_id: str
    ) -> tuple[str, str]:
        state = self._skill_state.get(conversation_id) or {}
        pending = state.get("pending")
        if not pending:
            return "There's nothing pending to confirm.", "skill_confirm"
        state["pending"] = None
        request_id_str, token, identifier, action = pending
        from uuid import UUID as _UUID

        try:
            result = await self._action_runtime.confirm(_UUID(request_id_str), token, payload["approved"])
        except Exception as exc:
            return f"I couldn't complete that: {exc}", "skill_confirm"

        await self._record_decision(
            f"Skill {action} for {identifier}: {result.status.value} (user {'approved' if payload['approved'] else 'declined'}).",
            conversation_id,
        )
        if not payload["approved"]:
            return "Okay, cancelled.", "skill_confirm"
        if result.status != ActionStatus.SUCCEEDED:
            return f"That didn't succeed: {result.error or result.summary}", "skill_confirm"

        if action == "install_skill":
            state["last_identifier"] = identifier
            return f"Installed `{identifier}` into your Obsidian skill vault.", "skill_confirm"
        if action == "uninstall_skill":
            if state.get("last_identifier") == identifier:
                state["last_identifier"] = None
            return f"Removed `{identifier}`.", "skill_confirm"
        return result.summary, "skill_confirm"

    async def _handle_skill_use(
        self, payload: dict[str, Any], conversation_id: str
    ) -> tuple[str, str]:
        target = payload.get("target")
        task = payload.get("task") or ""
        if target:
            identifier = await self._resolve_installed_target(target, conversation_id)
        else:
            state = self._skill_state.get(conversation_id) or {}
            identifier = state.get("last_identifier")
        if identifier is None:
            return "I don't know which skill to use -- install one first or name it explicitly.", "skill_use"

        result = await self._dispatch_skill("use_skill", {"identifier": identifier, "task": task}, conversation_id)
        if result.status != ActionStatus.SUCCEEDED:
            return f"I couldn't load that skill: {result.error}", "skill_use"

        self._skill_state.setdefault(conversation_id, {})["last_identifier"] = identifier
        skill_content = result.data["content"]
        # ClaudeCodeProvider passes the whole prompt as a single argv
        # element to the `claude` CLI subprocess -- observed directly: a
        # ~15.6KB real SKILL.md pushed that past Windows's effective
        # command-line length limit ("The command line is too long"),
        # failing the whole turn. Capping here (not in ClaudeCodeProvider
        # itself, which stays untouched per this task's constraints) keeps
        # every skill usable regardless of its instructions' length.
        if len(skill_content) > _SKILL_CONTENT_PROMPT_CAP:
            skill_content = skill_content[:_SKILL_CONTENT_PROMPT_CAP] + "\n\n[...instructions truncated for length...]"
        # The skill's instructions become grounding context for a real
        # assistant turn -- the underlying provider's own tool access is
        # unchanged by this; any real action it takes still goes through
        # its existing, separate permission-gated path, not this skill.
        #
        # Deliberately NOT phrased as "Using the installed skill `X`. Its
        # instructions: ..." -- observed directly: that phrasing collides
        # with Claude Code's own native skill-recognition, which then
        # treats it as an actual skill invocation, decides the named skill
        # has "empty content", and ignores the real instructions entirely.
        # Presenting it as plain reference material sidesteps that.
        augmented = (
            f"Follow these domain-specific instructions to help with the task below:\n\n"
            f"{skill_content}\n\n"
            f"---\nTask: {task or 'Apply the instructions above.'}"
        )
        # handle_text() (non-streaming) always calls the provider's strict
        # `.respond()` (--output-format json) -- observed directly: with
        # this kind of open-ended, instructions-heavy prompt, Claude
        # sometimes answers in plain prose instead of the required JSON
        # envelope, which .respond() then rejects as "did not return valid
        # JSON output" and the whole turn fails. handle_text_stream() uses
        # the more tolerant `.respond_stream()` (stream-json) when the
        # provider supports it, which doesn't require the model's own
        # answer to be JSON -- same provider, same model, just a less
        # brittle transport for this call shape.
        final_message: dict[str, Any] = {}
        async for event in self._assistant_router.handle_text_stream(augmented, conversation_id):
            if event.get("type") == "complete":
                final_message = event.get("message", {})
        # .get(..., default) only covers a missing key -- a genuinely
        # empty string (observed directly: the CLI can exit 0 with no
        # assistant text for some prompts) needs its own fallback so the
        # user never sees a blank reply.
        return final_message.get("content") or "I loaded the skill but didn't get a usable response back -- try rephrasing the task.", "skill_use"

    # ---- skill registry shared helpers ------------------------------------

    async def _dispatch_skill(self, action: str, arguments: dict[str, Any], conversation_id: str):
        request = ActionRequest(
            skill="skills",
            action=action,
            arguments=arguments,
            source=ActionSource.deterministic,
        )
        result = await self._action_runtime.submit(request)
        await self._record_decision(
            f"Dispatched skills.{action}() via the orchestrator: {result.status.value}.",
            conversation_id,
        )
        return result

    async def _resolve_installed_target(self, target: str, conversation_id: str) -> str | None:
        """Resolves a spoken/typed name/topic to an installed skill's exact
        identifier -- never guesses among the full 90k catalog, only among
        what's actually installed."""
        if not target:
            state = self._skill_state.get(conversation_id) or {}
            return state.get("last_identifier")
        result = await self._dispatch_skill("list_installed", {}, conversation_id)
        if result.status != ActionStatus.SUCCEEDED:
            return None
        target_lower = target.strip().lower()
        for skill in result.data.get("installed", []):
            if skill["identifier"] == target or target_lower in skill["name"].lower():
                return skill["identifier"]
        return None

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
