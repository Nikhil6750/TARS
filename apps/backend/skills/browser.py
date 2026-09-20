"""`browser` skill.

`open_url`/`search` open the user's real default OS browser via the stdlib
`webbrowser` module -- no headless automation, no scraping, per
M2A_SPEC.md's "no fake browser automation."

Wave 2B adds DOM-level control of the app's own embedded browser panel
(`navigate`, `inspect_dom`, `read_text`, `scroll`, `click`, `type`,
`back`, `forward`). That DOM lives in the renderer process, which this
backend has no direct access to -- so these actions are validated and
risk-classified here exactly like any other action, then physically
carried out via `actions.frontend_bridge.FrontendCommandBridge`, which
dispatches the already-authorized command to the connected frontend and
waits for a truthful report of what happened. The frontend never
independently decides to run, skip, or reclassify one of these commands;
see `apps/web/src/services/browser-control.ts`'s `frontend_command`
handler, which performs exactly the dispatched action and nothing else.
"""
from __future__ import annotations

import re
import webbrowser
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote_plus, urlsplit

from actions.frontend_bridge import FrontendBridgeError, FrontendCommandBridge
from app.action_contracts import (
    ActionRequest,
    ActionResult,
    ActionStatus,
    BaseSkill,
    RiskLevel,
    SkillExecutionError,
    SkillValidationError,
)

_ALLOWED_SCHEMES = {"http", "https"}
_SEARCH_URL_TEMPLATE = "https://www.google.com/search?q={query}"

# DOM actions that must physically execute in the renderer, dispatched via
# the frontend bridge rather than any local Python I/O.
_DOM_ACTIONS = {
    "navigate",
    "inspect_dom",
    "read_text",
    "scroll",
    "click",
    "type",
    "back",
    "forward",
}

_STATE_CHANGING_TARGET = re.compile(
    r"submit|order|buy|purchase|delete|confirm|pay|checkout", re.IGNORECASE
)
_SENSITIVE_FIELD = re.compile(r"password|secret|card|token|cvv|ssn|pin\b", re.IGNORECASE)

_DEFAULT_BRIDGE_TIMEOUT = 15.0


def validate_http_url(raw_url: str) -> str:
    """Returns the validated URL string, or raises SkillValidationError.
    Only syntactically valid http/https URLs with a network location are
    accepted -- rejects file://, javascript:, data:, and any other scheme
    that could read local files or execute script instead of opening a
    page."""
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise SkillValidationError("url must be a non-empty string")
    raw_url = raw_url.strip()
    parts = urlsplit(raw_url)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise SkillValidationError(
            f"url scheme '{parts.scheme or '(none)'}' is not allowed -- only http/https"
        )
    if not parts.netloc:
        raise SkillValidationError(f"url '{raw_url}' has no host")
    return raw_url


class BrowserSkill(BaseSkill):
    name = "browser"
    description = "Control the connected browser/embedded-DOM: navigate, open URLs, read/inspect page content."
    capabilities: tuple[str, ...] = (
        "open_url",
        "search",
        "navigate",
        "inspect_dom",
        "read_text",
        "scroll",
        "click",
        "type",
        "back",
        "forward",
    )

    def __init__(self, bridge: FrontendCommandBridge | None = None) -> None:
        self._bridge = bridge

    async def health(self) -> dict[str, Any]:
        return {"available": self._bridge is not None, "requires": "FrontendCommandBridge"}

    def classify_risk(self, action: str, arguments: dict[str, Any]) -> RiskLevel:
        if action in ("open_url", "search", "navigate", "back", "forward"):
            return RiskLevel.LOW_RISK
        if action in ("inspect_dom", "read_text", "scroll"):
            return RiskLevel.READ_ONLY
        if action == "click":
            target = str(arguments.get("target") or arguments.get("selector") or "")
            return (
                RiskLevel.CONFIRM_REQUIRED
                if _STATE_CHANGING_TARGET.search(target)
                else RiskLevel.LOW_RISK
            )
        if action == "type":
            if arguments.get("is_sensitive"):
                return RiskLevel.CONFIRM_REQUIRED
            selector = str(arguments.get("selector") or "")
            return (
                RiskLevel.CONFIRM_REQUIRED
                if _SENSITIVE_FIELD.search(selector)
                else RiskLevel.LOW_RISK
            )
        return RiskLevel.BLOCKED

    async def validate(self, action: str, arguments: dict[str, Any]) -> None:
        if action == "open_url":
            validate_http_url(arguments.get("url", ""))
        elif action == "search":
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                raise SkillValidationError("search requires non-empty 'query'")
        elif action == "navigate":
            validate_http_url(arguments.get("url", ""))
        elif action in ("inspect_dom", "back", "forward"):
            return
        elif action == "read_text":
            mode = arguments.get("mode", "summary")
            if mode not in ("all", "selection", "summary", "headings"):
                raise SkillValidationError("'mode' must be all/selection/summary/headings")
        elif action == "scroll":
            delta = arguments.get("deltaY")
            if delta not in ("top", "bottom", "element") and not isinstance(delta, (int, float)):
                raise SkillValidationError(
                    "'deltaY' must be a number or one of top/bottom/element"
                )
        elif action == "click":
            target = arguments.get("target") or arguments.get("selector")
            if not isinstance(target, str) or not target.strip():
                raise SkillValidationError("click requires non-empty 'target' or 'selector'")
        elif action == "type":
            selector = arguments.get("selector")
            text = arguments.get("text")
            if not isinstance(selector, str) or not selector.strip():
                raise SkillValidationError("type requires non-empty 'selector'")
            if not isinstance(text, str):
                raise SkillValidationError("type requires string 'text'")
        else:
            raise SkillValidationError(f"unsupported browser action '{action}'")

    async def execute(self, request: ActionRequest) -> ActionResult:
        started = datetime.now(UTC)
        if request.action == "open_url":
            url = validate_http_url(request.arguments["url"])
            return self._open_external(
                request, url, started, summary=f"Opened {url} in the default browser."
            )
        if request.action == "search":
            query = request.arguments["query"]
            url = _SEARCH_URL_TEMPLATE.format(query=quote_plus(query))
            return self._open_external(
                request,
                url,
                started,
                summary=f"Opened a web search for '{query}' in the default browser.",
            )
        if request.action in _DOM_ACTIONS:
            return await self._execute_dom_action(request, started)
        raise SkillExecutionError(f"unsupported browser action '{request.action}'")

    def _open_external(
        self, request: ActionRequest, url: str, started: datetime, *, summary: str
    ) -> ActionResult:
        try:
            opened = webbrowser.open(url)
        except Exception as exc:
            raise SkillExecutionError(f"failed to open browser for '{url}': {exc}") from exc

        if not opened:
            return self._result(
                request,
                ActionStatus.FAILED,
                f"No browser handler available to open {url}.",
                risk_level=RiskLevel.LOW_RISK,
                error="webbrowser.open returned False (no browser controller found)",
                started_at=started,
            )

        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            summary,
            risk_level=RiskLevel.LOW_RISK,
            data={"url": url},
            started_at=started,
        )

    async def _execute_dom_action(self, request: ActionRequest, started: datetime) -> ActionResult:
        if self._bridge is None:
            raise SkillExecutionError(
                f"browser.{request.action}() requires a connected frontend and no "
                "FrontendCommandBridge is wired -- refusing rather than fabricating a result"
            )
        risk = self.classify_risk(request.action, request.arguments)
        try:
            data = await self._bridge.dispatch(
                request.id,
                self.name,
                request.action,
                request.arguments,
                timeout=_DEFAULT_BRIDGE_TIMEOUT,
            )
        except FrontendBridgeError as exc:
            return self._result(
                request,
                ActionStatus.FAILED,
                f"browser.{request.action}() did not complete: {exc}",
                risk_level=risk,
                error=str(exc),
                started_at=started,
            )
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            str(data.get("summary") or f"Executed browser.{request.action}()."),
            risk_level=risk,
            data=data,
            started_at=started,
        )
