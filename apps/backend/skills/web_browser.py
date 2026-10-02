"""`web` skill -- BrowserAgent for real websites (YouTube, Google, etc.) in a
real, CDP-connected Chrome instance.

Distinct from `skills/browser.py` (TARS's own embedded dashboard webview,
driven through `actions/frontend_bridge.py`): this skill's `BrowserSession`
(`browser/session.py`) talks Chrome DevTools Protocol directly to a real
browser process. Tabs, navigation, and the semantic click/type/select below
all resolve against the real DOM via `browser/element_resolver.py` -- never
screen coordinates, never a vision call. CDP also means these actions work
whether or not the Chrome window is focused or even visible (see
`browser/session.py`'s module docstring) -- TARS does not need to steal
foreground focus to drive it, unlike UIA desktop control or TradingView's
keystroke-simulation skill.

Every action is verified against the resulting page state (URL/readyState
for navigation, the clicked/typed element's own post-action state for
click/type) rather than assumed successful because a command was sent --
see `BrowserSession`'s per-method docstrings/`element_resolver.py` for what
"verified" means for each action. A resolution miss is reported as
`outcome: NOT_FOUND`/`AMBIGUOUS` in `data`, never silently retried against a
guessed coordinate.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from app.action_contracts import (
    ActionRequest,
    ActionResult,
    ActionStatus,
    BaseSkill,
    RiskLevel,
    SkillExecutionError,
    SkillValidationError,
)
from browser.bridge_server import BridgeNotConnected
from browser.cdp import CDPError
from browser.provider import BrowserProvider
from browser.session import BrowserLaunchError

# Same heuristics `skills/browser.py` uses for the embedded webview, applied
# to the *requested* target description (classify_risk runs before
# resolution, so it only ever sees what the caller asked for, e.g. "the buy
# now button" -- never the resolved element, which does not exist yet).
_STATE_CHANGING_TARGET = re.compile(
    r"submit|order|buy|purchase|delete|confirm|pay|checkout|send\b", re.IGNORECASE
)
_SENSITIVE_FIELD = re.compile(r"password|secret|card number|cvv|ssn|pin\b", re.IGNORECASE)

_SCROLL_DIRECTIONS = ("up", "down", "top", "bottom")
_EXTRACT_MODES = ("all", "summary", "headings")
_MAX_WAIT_TIMEOUT = 30.0


class WebBrowserSkill(BaseSkill):
    name = "web"
    description = "BrowserAgent: real Chrome tabs/navigation/semantic click-type-extract via CDP."
    capabilities: tuple[str, ...] = (
        "get_context", "list_tabs", "focus_tab", "new_tab", "close_tab",
        "navigate", "back", "forward", "refresh",
        "find", "click", "type", "select", "scroll", "wait_for",
        "extract_text", "extract_table", "get_links",
        "download", "get_last_download",
    )

    def __init__(self, session: BrowserProvider | None = None) -> None:
        self._session = session or BrowserProvider()

    async def health(self) -> dict[str, Any]:
        return {"available": True, "active_provider": self._session.active_provider_name,
                "note": "Chrome (CDP) is launched on first use, not at startup; the browser bridge "
                        "extension, if paired, is preferred automatically when connected."}

    def classify_risk(self, action: str, arguments: dict[str, Any]) -> RiskLevel:
        if action in ("get_context", "list_tabs", "find", "extract_text", "extract_table", "get_links",
                      "get_last_download"):
            return RiskLevel.READ_ONLY
        if action == "download":
            return RiskLevel.CONFIRM_REQUIRED
        if action == "click":
            target = str(arguments.get("target") or "")
            return RiskLevel.CONFIRM_REQUIRED if _STATE_CHANGING_TARGET.search(target) else RiskLevel.LOW_RISK
        if action == "type":
            target = str(arguments.get("target") or "")
            if arguments.get("is_sensitive") or _SENSITIVE_FIELD.search(target):
                return RiskLevel.CONFIRM_REQUIRED
            return RiskLevel.LOW_RISK
        if action in ("focus_tab", "new_tab", "close_tab", "navigate", "back", "forward", "refresh",
                      "select", "scroll", "wait_for"):
            return RiskLevel.LOW_RISK
        return RiskLevel.BLOCKED

    async def validate(self, action: str, arguments: dict[str, Any]) -> None:
        if action in ("get_context", "list_tabs", "back", "forward", "refresh", "get_links"):
            return
        if action in ("focus_tab", "find", "click"):
            _require_str(arguments, "target")
        elif action == "new_tab":
            _optional_str(arguments, "url")
        elif action == "close_tab":
            _optional_str(arguments, "target")
        elif action == "navigate":
            _require_str(arguments, "url")
        elif action == "type":
            _require_str(arguments, "target")
            if not isinstance(arguments.get("text"), str):
                raise SkillValidationError("type requires string 'text'")
        elif action == "select":
            _require_str(arguments, "target")
            _require_str(arguments, "value")
        elif action == "scroll":
            direction = arguments.get("direction", "down")
            if direction not in _SCROLL_DIRECTIONS:
                raise SkillValidationError(f"'direction' must be one of {_SCROLL_DIRECTIONS}")
            _optional_str(arguments, "target")
        elif action == "wait_for":
            _require_str(arguments, "target")
            timeout = arguments.get("timeout", 10.0)
            if not isinstance(timeout, (int, float)) or not (0 < timeout <= _MAX_WAIT_TIMEOUT):
                raise SkillValidationError(f"'timeout' must be a number between 0 and {_MAX_WAIT_TIMEOUT}")
        elif action == "extract_text":
            mode = arguments.get("mode", "summary")
            if mode not in _EXTRACT_MODES:
                raise SkillValidationError(f"'mode' must be one of {_EXTRACT_MODES}")
        elif action == "extract_table":
            _optional_str(arguments, "target")
        elif action == "download":
            _require_str(arguments, "target")
        elif action == "get_last_download":
            return
        else:
            raise SkillValidationError(f"unsupported web action '{action}'")

    async def execute(self, request: ActionRequest) -> ActionResult:
        started = datetime.now(UTC)
        action, args = request.action, request.arguments
        try:
            if action == "get_context":
                data = await self._session.get_context()
                return self._result(request, ActionStatus.SUCCEEDED, f"Showing {data.get('title') or data.get('url')}.",
                                    risk_level=RiskLevel.READ_ONLY, data=data, started_at=started)
            if action == "list_tabs":
                tabs = await self._session.list_tabs()
                return self._result(request, ActionStatus.SUCCEEDED, f"{len(tabs)} tab(s) open.",
                                    risk_level=RiskLevel.READ_ONLY, data={"tabs": tabs}, started_at=started)
            if action == "focus_tab":
                return self._from_lookup(request, started, await self._session.focus_tab(args["target"]),
                                         ok_summary=lambda d: f"Switched to tab '{d.get('title') or d.get('url')}'.")
            if action == "new_tab":
                out = await self._session.new_tab(args.get("url", ""))
                return self._result(request, ActionStatus.SUCCEEDED, f"Opened a new tab at {out.get('url')}.",
                                    risk_level=RiskLevel.LOW_RISK, data=out, started_at=started)
            if action == "close_tab":
                return self._from_lookup(request, started, await self._session.close_tab(args.get("target", "")),
                                         ok_summary=lambda d: "Closed the tab.")
            if action == "navigate":
                return self._execute_navigate(request, started, await self._session.navigate(args["url"]))
            if action == "back":
                return self._execute_history(request, started, await self._session.back(), "back")
            if action == "forward":
                return self._execute_history(request, started, await self._session.forward(), "forward")
            if action == "refresh":
                out = await self._session.refresh()
                status = ActionStatus.SUCCEEDED if out["ok"] else ActionStatus.FAILED
                return self._result(request, status, f"Refreshed '{out.get('title') or out.get('url')}'."
                                    if out["ok"] else "Refresh did not finish loading in time.",
                                    risk_level=RiskLevel.LOW_RISK, data=out, started_at=started)
            if action == "find":
                return self._from_resolution(request, started, await self._session.find(args["target"]), args, verb="find")
            if action == "click":
                return self._from_resolution(request, started, await self._session.click(args["target"]), args, verb="click")
            if action == "type":
                sensitive = bool(args.get("is_sensitive")) or bool(_SENSITIVE_FIELD.search(args["target"]))
                out = await self._session.type_text(args["target"], args["text"],
                                                    clear=args.get("clear", True), submit=bool(args.get("submit")))
                return self._from_resolution(request, started, out, args, verb="type into", sensitive=sensitive)
            if action == "select":
                out = await self._session.select(args["target"], args["value"])
                return self._from_resolution(request, started, out, args, verb="select")
            if action == "scroll":
                if args.get("target"):
                    out = await self._session.scroll_to(args["target"])
                    return self._from_resolution(request, started, out, args, verb="scroll to")
                out = await self._session.scroll(args.get("direction", "down"))
                return self._result(request, ActionStatus.SUCCEEDED, f"Scrolled {out['direction']}.",
                                    risk_level=RiskLevel.READ_ONLY, data=out, started_at=started)
            if action == "wait_for":
                out = await self._session.wait_for(args["target"], timeout=float(args.get("timeout", 10.0)))
                return self._from_resolution(request, started, out, args, verb="wait for")
            if action == "extract_text":
                out = await self._session.extract_text(args.get("mode", "summary"))
                return self._result(request, ActionStatus.SUCCEEDED, "Extracted page text.",
                                    risk_level=RiskLevel.READ_ONLY, data=out, started_at=started)
            if action == "extract_table":
                out = await self._session.extract_table(args.get("target", ""))
                status = ActionStatus.SUCCEEDED if out.get("ok") else ActionStatus.FAILED
                return self._result(request, status,
                                    f"Extracted a table with {len(out.get('rows', []))} row(s)." if out.get("ok")
                                    else "No table found on the page.",
                                    risk_level=RiskLevel.READ_ONLY, data=out, started_at=started)
            if action == "get_links":
                out = await self._session.get_links()
                return self._result(request, ActionStatus.SUCCEEDED, f"Found {len(out['links'])} link(s).",
                                    risk_level=RiskLevel.READ_ONLY, data=out, started_at=started)
            if action == "download":
                return self._execute_download(request, started, args.get("target", ""),
                                              await self._session.download(args["target"]))
            if action == "get_last_download":
                out = self._session.get_last_download()
                status = ActionStatus.SUCCEEDED if out.get("ok") else ActionStatus.FAILED
                return self._result(request, status,
                                    f"Last download: {out.get('filename')} at {out.get('path')}." if out.get("ok")
                                    else "Nothing has been downloaded yet this session.",
                                    risk_level=RiskLevel.READ_ONLY, data=out, started_at=started)
        except BrowserLaunchError as exc:
            return self._result(request, ActionStatus.FAILED, f"Could not start the browser: {exc}",
                                risk_level=RiskLevel.LOW_RISK, error=str(exc),
                                data={"outcome": "NOT_FOUND"}, started_at=started)
        except BridgeNotConnected as exc:
            # Narrow race: BrowserProvider saw the bridge connected when it
            # chose a provider, but the extension disconnected before the
            # call landed. Reported honestly rather than silently retried
            # against CDP mid-action, which could duplicate a state-changing
            # action (a click, a submit) on the wrong tab/page.
            return self._result(request, ActionStatus.FAILED, f"The paired browser disconnected: {exc}",
                                risk_level=RiskLevel.LOW_RISK, error=str(exc),
                                data={"outcome": "FAILED"}, started_at=started)
        except CDPError as exc:
            return self._result(request, ActionStatus.FAILED, f"Browser control failed: {exc}",
                                risk_level=RiskLevel.LOW_RISK, error=str(exc),
                                data={"outcome": "FAILED"}, started_at=started)
        raise SkillExecutionError(f"unsupported web action '{action}'")

    # ---- shared result shaping --------------------------------------------
    def _from_lookup(self, request: ActionRequest, started: datetime, out: dict, *, ok_summary) -> ActionResult:
        if out.get("ok"):
            return self._result(request, ActionStatus.SUCCEEDED, ok_summary(out),
                                risk_level=RiskLevel.LOW_RISK, data={**out, "outcome": "SUCCESS"}, started_at=started)
        return self._result(request, ActionStatus.FAILED, "No matching tab was found.",
                            risk_level=RiskLevel.LOW_RISK, error="tab not found",
                            data={**out, "outcome": "NOT_FOUND"}, started_at=started)

    def _execute_navigate(self, request: ActionRequest, started: datetime, out: dict) -> ActionResult:
        if out["ok"]:
            return self._result(request, ActionStatus.SUCCEEDED, f"Navigated to '{out.get('title') or out['url']}'.",
                                risk_level=RiskLevel.LOW_RISK, data={**out, "outcome": "SUCCESS"}, started_at=started)
        from urllib.parse import urlsplit

        same_host = urlsplit(out.get("url", "")).netloc == urlsplit(out["requested"]).netloc
        outcome = "PARTIAL" if same_host and out.get("url") else "NOT_VERIFIED"
        return self._result(
            request, ActionStatus.FAILED,
            f"Asked the browser to go to {out['requested']}, but it did not finish loading in time.",
            risk_level=RiskLevel.LOW_RISK, error="navigation not verified",
            data={**out, "outcome": outcome}, started_at=started,
        )

    def _execute_history(self, request: ActionRequest, started: datetime, out: dict, direction: str) -> ActionResult:
        if out["ok"]:
            return self._result(request, ActionStatus.SUCCEEDED, f"Went {direction} to '{out.get('title') or out['url']}'.",
                                risk_level=RiskLevel.LOW_RISK, data={**out, "outcome": "SUCCESS"}, started_at=started)
        if not out.get("changed"):
            return self._result(request, ActionStatus.FAILED, f"There is nothing to go {direction} to.",
                                risk_level=RiskLevel.LOW_RISK, error=f"no {direction} history",
                                data={**out, "outcome": "NOT_FOUND"}, started_at=started)
        return self._result(request, ActionStatus.FAILED, f"Went {direction}, but the page did not finish loading in time.",
                            risk_level=RiskLevel.LOW_RISK, error="navigation not verified",
                            data={**out, "outcome": "PARTIAL"}, started_at=started)

    def _execute_download(self, request: ActionRequest, started: datetime, target: str, out: dict) -> ActionResult:
        status_word = out.get("status", "FAILED")
        if status_word == "COMPLETED":
            return self._result(
                request, ActionStatus.SUCCEEDED,
                f"Downloaded '{out['filename']}' ({out['size_bytes']} bytes) to {out['path']}.",
                risk_level=RiskLevel.CONFIRM_REQUIRED, data={**out, "outcome": "SUCCESS"}, started_at=started,
            )
        if status_word == "DOWNLOADING":
            return self._result(
                request, ActionStatus.FAILED, f"Still downloading '{target}' -- not finished yet.",
                risk_level=RiskLevel.CONFIRM_REQUIRED, error="download in progress",
                data={**out, "outcome": "PARTIAL"}, started_at=started,
            )
        reason = out.get("reason", "FAILED")
        if reason in ("NOT_FOUND", "AMBIGUOUS"):
            return self._result(
                request, ActionStatus.FAILED, f"Could not download '{target}': {reason.lower().replace('_', ' ')}.",
                risk_level=RiskLevel.CONFIRM_REQUIRED, error=reason,
                data={**out, "outcome": reason}, started_at=started,
            )
        return self._result(
            request, ActionStatus.FAILED,
            f"Clicked '{target}' but no download started within the timeout.",
            risk_level=RiskLevel.CONFIRM_REQUIRED, error="no download started",
            data={**out, "outcome": "FAILED"}, started_at=started,
        )

    def _from_resolution(
        self, request: ActionRequest, started: datetime, out: dict, args: dict, *, verb: str, sensitive: bool = False
    ) -> ActionResult:
        target = args.get("target", "")
        if out.get("ok"):
            matched = dict(out.get("matched") or {})
            if sensitive:
                matched["text"] = "[REDACTED]"
                out = {**out, "value_after": "[REDACTED]"} if "value_after" in out else out
            desc = matched.get("name") or matched.get("text") or matched.get("tag") or target
            # A click on a real link is only a claim of success at the DOM
            # level (event dispatched) until BrowserSession's own brief poll
            # confirms the page actually changed -- confirmed live that some
            # sites (Google's result-redirect links) take a couple of
            # seconds, so "navigated" being present and False means the
            # click happened but nothing was observed to follow from it yet.
            if "navigated" in out:
                if out["navigated"]:
                    summary = f"{verb.capitalize()} '{desc}', which opened '{out.get('title_after') or out.get('url_after')}'."
                    outcome = "SUCCESS"
                else:
                    summary = f"{verb.capitalize()} '{desc}', but no resulting page change was observed."
                    outcome = "PARTIAL"
            else:
                summary, outcome = f"{verb.capitalize()} '{desc}'.", "SUCCESS"
            return self._result(
                request, ActionStatus.SUCCEEDED if outcome == "SUCCESS" else ActionStatus.FAILED, summary,
                risk_level=RiskLevel.LOW_RISK if verb != "find" else RiskLevel.READ_ONLY,
                data={**out, "matched": matched, "outcome": outcome}, started_at=started,
            )
        reason = out.get("reason", "NOT_FOUND")
        if reason == "SCRIPT_ERROR":
            raise SkillExecutionError(f"page script failed while trying to {verb} '{target}': {out.get('error')}")
        if reason == "AMBIGUOUS":
            names = [c.get("name") or c.get("text") or c.get("tag") for c in out.get("candidates", [])]
            return self._result(
                request, ActionStatus.FAILED, f"'{target}' is ambiguous -- {len(names)} elements match: {names[:3]}.",
                risk_level=RiskLevel.LOW_RISK, error="ambiguous target",
                data={**out, "outcome": "AMBIGUOUS"}, started_at=started,
            )
        return self._result(
            request, ActionStatus.FAILED, f"Could not find '{target}' on the current page.",
            risk_level=RiskLevel.LOW_RISK, error="target not found",
            data={**out, "outcome": "NOT_FOUND"}, started_at=started,
        )


def _require_str(arguments: dict[str, Any], key: str) -> None:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SkillValidationError(f"requires non-empty string '{key}'")


def _optional_str(arguments: dict[str, Any], key: str) -> None:
    value = arguments.get(key)
    if value is not None and not isinstance(value, str):
        raise SkillValidationError(f"'{key}' must be a string if provided")
