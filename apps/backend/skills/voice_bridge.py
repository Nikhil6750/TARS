"""Deterministic voice-phrase -> ActionRequest bridge.

Mirrors the deterministic-vs-model routing pattern in
`apps/backend/assistant/router.py` (`AssistantRouter._try_deterministic`):
a small, fixed set of regex patterns recognizes a handful of action phrases
without any LLM call, per M2A_SPEC.md acceptance criterion 12. Anything not
matched returns `None`, meaning "not a deterministic action" -- the caller
(the voice pipeline / action runtime, not this module) is responsible for
falling through to the existing assistant/LLM path unchanged.

This is intentionally small: a handful of phrases, not a general NLU
system. It does not execute anything itself -- it only constructs an
`ActionRequest` for the action runtime to validate/classify/dispatch, same
as any HUD- or hotkey-issued request.
"""
from __future__ import annotations

import re
from typing import Any

from app.action_contracts import ActionRequest, ActionSource, ActiveWindowContext
from skills.browser import validate_http_url

_FOCUS_PATTERN = re.compile(r"^\s*focus\s+(?:on\s+)?(.+?)\s*$", re.IGNORECASE)
_OPEN_URL_PATTERN = re.compile(r"^\s*open\s+(https?://\S+)\s*$", re.IGNORECASE)
_LAUNCH_PATTERN = re.compile(r"^\s*(?:launch|open|start)\s+(.+?)\s*$", re.IGNORECASE)
_WEB_SEARCH_PATTERN = re.compile(
    r"^\s*search\s+(?:the\s+web\s+)?for\s+(.+?)\s*$", re.IGNORECASE
)
# Distinct verbs ("run"/"execute"/"terminal") from the other patterns above,
# so this cannot shadow or be shadowed by focus/open/launch/search matching.
# The command itself is still fully re-classified by the permission engine
# (READ_ONLY / CONFIRM_REQUIRED / BLOCKED) exactly like any other terminal
# request -- this only builds the ActionRequest, same as every other branch.
_TERMINAL_PATTERN = re.compile(
    r"^\s*(?:run(?:\s+command)?|execute|terminal)\s*:?\s+(.+?)\s*$", re.IGNORECASE
)


def build_action_request_from_voice(
    text: str,
    *,
    source: ActionSource,
    active_context: ActiveWindowContext | None = None,
) -> ActionRequest | None:
    """Returns a constructed ActionRequest for a recognized deterministic
    action phrase, or None if `text` isn't one (falls through to the
    existing LLM/assistant path, unchanged, outside this function's
    concern)."""
    if not text or not text.strip():
        return None

    # Strip trailing sentence punctuation STT commonly appends (e.g. "Open
    # Notepad." from real speech-to-text) -- left in place, it becomes part
    # of the captured target/query and fails downstream (e.g. "Notepad."
    # never resolves via shutil.which, only "Notepad" does).
    text = re.sub(r"[.!?]+$", "", text.strip()).strip()
    if not text:
        return None

    if match := _FOCUS_PATTERN.match(text):
        target = match.group(1).strip()
        if target:
            return _build(
                skill="windows_app",
                action="focus",
                arguments={"target": target},
                source=source,
                active_context=active_context,
            )

    if match := _OPEN_URL_PATTERN.match(text):
        url = match.group(1).strip()
        try:
            validate_http_url(url)
        except Exception:
            return None
        return _build(
            skill="browser",
            action="open_url",
            arguments={"url": url},
            source=source,
            active_context=active_context,
        )

    if match := _WEB_SEARCH_PATTERN.match(text):
        query = match.group(1).strip()
        if query:
            return _build(
                skill="browser",
                action="search",
                arguments={"query": query},
                source=source,
                active_context=active_context,
            )

    if match := _LAUNCH_PATTERN.match(text):
        target = match.group(1).strip()
        if target:
            return _build(
                skill="windows_app",
                action="launch",
                arguments={"target": target},
                source=source,
                active_context=active_context,
            )

    if match := _TERMINAL_PATTERN.match(text):
        command = match.group(1).strip()
        if command:
            return _build(
                skill="terminal",
                action="run_command",
                arguments={"command": command},
                source=source,
                active_context=active_context,
            )

    return None


def _build(
    *,
    skill: str,
    action: str,
    arguments: dict[str, Any],
    source: ActionSource,
    active_context: ActiveWindowContext | None,
) -> ActionRequest:
    return ActionRequest(
        skill=skill,
        action=action,
        arguments=arguments,
        source=source,
        active_context=active_context,
    )
