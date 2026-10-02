"""Shared ordinal/relative/pronoun resolution bookkeeping (mission sections
1-2) and bounded recovery retry (section 11), reused by every BrowserAgent
provider -- `browser/session.py`'s CDP-backed `BrowserSession` and
`browser/bridge_session.py`'s extension-backed `BridgeSession`.

Resolving "the second video"/"the next one"/"it" and retrying a transient
NOT_FOUND once is identical logic regardless of which transport actually
reaches into the page (CDP's `Runtime.evaluate` vs the extension's
`chrome.scripting.executeScript`) -- only that one primitive differs, so it
lives here once and each provider supplies it via `_execute()`. This is
also what makes mission section 8's "single contract" true in practice:
both providers return the exact same shape of result for the exact same
reason, because both run through this.
"""
from __future__ import annotations

import asyncio
from typing import Any

from browser.element_resolver import parse_ordinal

RESOLUTION_RETRY_ATTEMPTS = 2  # initial attempt + 1 bounded retry
RESOLUTION_RETRY_DELAY = 0.5


class ElementResolutionMixin:
    """Mixed into a provider session class. Requires the subclass to
    provide `async def _execute(self, js_action, js_target, args) -> dict`
    (the actual DOM round trip) and to call `_init_resolution_state()` in
    its own `__init__` and `_forget_relative_references()` wherever it
    navigates/goes back/forward/refreshes."""

    def _init_resolution_state(self) -> None:
        self._last_collection_hint: str | None = None
        self._last_ordinal: int | None = None
        self._last_resolved_descriptor: dict[str, Any] | None = None

    def _forget_relative_references(self) -> None:
        self._last_collection_hint = None
        self._last_ordinal = None
        self._last_resolved_descriptor = None

    async def _resolve_and_act(self, js_action: str, target: str, extra_args: dict[str, Any] | None = None) -> dict[str, Any]:
        query = parse_ordinal(target)
        args = dict(extra_args or {})

        if query.is_pronoun:
            if self._last_resolved_descriptor is None:
                return {"ok": False, "reason": "NOT_FOUND", "query": target,
                        "error": "nothing has been resolved yet to refer back to"}
            referent = self._last_resolved_descriptor.get("name") or self._last_resolved_descriptor.get("text") or ""
            if not referent:
                return {"ok": False, "reason": "NOT_FOUND", "query": target,
                        "error": "the last element had no text to re-find it by"}
            return await self._execute_with_recovery(js_action, referent, args)

        if query.relative or query.index is not None:
            if query.relative:
                if self._last_ordinal is None:
                    return {"ok": False, "reason": "NOT_FOUND", "query": target,
                            "error": f"no previous result to go '{query.relative}' from"}
                index = self._last_ordinal + (1 if query.relative == "next" else -1)
                if index < 0:
                    return {"ok": False, "reason": "NOT_FOUND", "query": target,
                            "error": "already at the first result"}
            else:
                index = query.index
            hint = query.hint or self._last_collection_hint
            args["ordinal"] = {"index": index, "hint": hint}
            return await self._execute_with_recovery(js_action, target, args)

        return await self._execute_with_recovery(js_action, target, args)

    async def _execute_with_recovery(self, js_action: str, js_target: str, args: dict[str, Any]) -> dict[str, Any]:
        """Mission section 11's bounded recovery for ELEMENT_NOT_FOUND/
        PAGE_CHANGED: one extra attempt after a short delay, re-resolving
        fresh against whatever the DOM looks like by then (never a cached
        node -- `_execute` always re-queries live). Never retries AMBIGUOUS
        (mission: "for ambiguous state, stop and ask") or a result that
        already succeeded."""
        result = await self._execute(js_action, js_target, args)
        attempts = 1
        while (not result.get("ok")) and result.get("reason") == "NOT_FOUND" and attempts < RESOLUTION_RETRY_ATTEMPTS:
            await asyncio.sleep(RESOLUTION_RETRY_DELAY)
            result = await self._execute(js_action, js_target, args)
            attempts += 1
        if attempts > 1:
            result["recovery_attempts"] = attempts
        return result

    def _remember_result(self, result: dict[str, Any]) -> None:
        if not result.get("ok"):
            return
        matched = result.get("matched")
        if matched:
            self._last_resolved_descriptor = matched
        diagnostics = result.get("diagnostics")
        if diagnostics and "ordinal" in diagnostics:
            self._last_ordinal = diagnostics["ordinal"]
            self._last_collection_hint = diagnostics.get("group") if diagnostics.get("group") != "(any)" else None

    async def _execute(self, js_action: str, js_target: str, args: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError
