"""Skill registry -- the boundary Codex's action runtime (`apps/backend/
actions/`) imports to dispatch a validated `ActionRequest.skill` to the
right implementation, per M2A_INTERFACES.md's "expected surface between
streams."

`SKILLS` covers the four skills that need no live app-state to construct
(`windows_app`, `filesystem`, `browser`, `terminal`) -- these are safe to
import and instantiate eagerly, same as any stateless module-level
singleton elsewhere in this codebase.

`obsidian` is deliberately NOT in the eager `SKILLS` dict: `ObsidianSkill`
wraps `memory.service.MemoryService`, which itself wraps a *live*
`aiosqlite.Connection` -- per `app/main.py`'s lifespan, that connection is
only opened during app startup (`app.state.db.conn`), the same DI pattern
`app/deps.py` uses for every other request-scoped singleton. Constructing
`MemoryService`/`ObsidianSkill` at import time would mean either opening an
unmanaged, untracked second DB connection (wrong -- `app/db.py`'s docstring
is explicit that a single shared connection is reused) or faking one just
to satisfy the constructor (which is exactly the kind of fabricated wiring
`AGENTS.md`/`M2A_SPEC.md` prohibit). `build_registry()` below is the real
integration point: call it once you have a `MemoryService` instance (e.g.
`request.app.state.memory_service` / `app.deps.get_memory_service`) to get
the full five-skill registry including `obsidian`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.action_contracts import Skill
from skills.browser import BrowserSkill
from skills.desktop_control import DesktopControlSkill
from skills.filesystem import FilesystemSkill
from skills.terminal import TerminalSkill
from skills.windows_app import WindowsAppSkill

if TYPE_CHECKING:
    from actions.frontend_bridge import FrontendCommandBridge
    from assistant.chart_analysis import ChartAnalysisService
    from memory.service import MemoryService
    from skill_registry.manager import SkillManager
    from trading.context import TradingContextBuilder


def build_registry(
    memory_service: MemoryService | None = None,
    vault_path: str | None = None,
    frontend_bridge: FrontendCommandBridge | None = None,
    chart_analysis_service: ChartAnalysisService | None = None,
    trading_context_builder: TradingContextBuilder | None = None,
    skill_manager: SkillManager | None = None,
) -> dict[str, Skill]:
    """Constructs the skill registry. Pass a live `MemoryService` (and
    optionally its vault path -- defaults to `get_settings().obsidian_vault_path`
    if omitted) to also include `obsidian`. Pass a live `FrontendCommandBridge`
    (see `actions/frontend_bridge.py`) so `browser`'s embedded-DOM actions and
    `windows_app`'s screen/monitor/UIA capture actions can actually reach the
    connected native shell; without one they validate and risk-classify
    normally but fail closed at execute() rather than fabricate a result.
    `chart_analysis_service`/`trading_context_builder` wire the `trading`
    skill's `analyze_active_chart`/`get_trading_context`/`explain_setup`
    actions the same way -- always registered, but any action needing an
    unwired dependency fails closed rather than fabricating a result (see
    skills/trading.py)."""
    skills: dict[str, Skill] = {
        "windows_app": WindowsAppSkill(bridge=frontend_bridge),
        "filesystem": FilesystemSkill(),
        "browser": BrowserSkill(bridge=frontend_bridge),
        "terminal": TerminalSkill(),
        "desktop_control": DesktopControlSkill(),
    }
    if memory_service is not None:
        from skills.obsidian import ObsidianSkill

        if vault_path is None:
            from app.config import get_settings

            vault_path = get_settings().obsidian_vault_path
        skills["obsidian"] = ObsidianSkill(memory_service, vault_path)

    from skills.trading import TradingSkill

    skills["trading"] = TradingSkill(
        memory_service=memory_service,
        chart_analysis_service=chart_analysis_service,
        context_builder=trading_context_builder,
        bridge=frontend_bridge,
    )

    from skills.skill_registry_skill import SkillRegistrySkill

    skills["skills"] = SkillRegistrySkill(manager=skill_manager)
    return skills


# Eagerly-constructed registry of the skills that need no live app.state
# dependency. Does NOT include "obsidian" -- see module docstring. The
# action runtime should call `build_registry(memory_service=...)` once it
# has its own `MemoryService` instance to get the complete registry.
SKILLS: dict[str, Skill] = build_registry()
