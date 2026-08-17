"""`browser` skill -- opens the user's real default browser via the stdlib
`webbrowser` module. No headless automation, no scraping, no fake browser
session -- literally opens a URL, per M2A_SPEC.md's "no fake browser
automation."
"""
from __future__ import annotations

import webbrowser
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote_plus, urlsplit

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
    capabilities: tuple[str, ...] = ("open_url", "search")

    def classify_risk(self, action: str, arguments: dict[str, Any]) -> RiskLevel:
        if action in ("open_url", "search"):
            return RiskLevel.LOW_RISK
        return RiskLevel.BLOCKED

    async def validate(self, action: str, arguments: dict[str, Any]) -> None:
        if action == "open_url":
            validate_http_url(arguments.get("url", ""))
        elif action == "search":
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                raise SkillValidationError("search requires non-empty 'query'")
        else:
            raise SkillValidationError(f"unsupported browser action '{action}'")

    async def execute(self, request: ActionRequest) -> ActionResult:
        started = datetime.now(UTC)
        if request.action == "open_url":
            url = validate_http_url(request.arguments["url"])
            return self._open(request, url, started, summary=f"Opened {url} in the default browser.")
        if request.action == "search":
            query = request.arguments["query"]
            url = _SEARCH_URL_TEMPLATE.format(query=quote_plus(query))
            return self._open(
                request, url, started, summary=f"Opened a web search for '{query}' in the default browser."
            )
        raise SkillExecutionError(f"unsupported browser action '{request.action}'")

    def _open(self, request: ActionRequest, url: str, started: datetime, *, summary: str) -> ActionResult:
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
