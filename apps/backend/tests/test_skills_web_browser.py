"""`web` skill tests -- validation, risk classification, and ActionResult
shaping, with a fake BrowserSession (same seam as test_browser_session.py's
fakes) so these exercise skills/web_browser.py's own logic, not the real
CDP/Chrome path."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.action_contracts import (
    ActionRequest,
    ActionSource,
    ActionStatus,
    RiskLevel,
    SkillExecutionError,
    SkillValidationError,
)
from browser.cdp import CDPError
from browser.session import BrowserLaunchError
from skills.web_browser import WebBrowserSkill


class _FakeSession:
    """Every method is an AsyncMock so a test configures only the one(s) it needs."""

    def __init__(self):
        for name in (
            "get_context", "list_tabs", "focus_tab", "new_tab", "close_tab",
            "navigate", "back", "forward", "refresh",
            "find", "click", "type_text", "select", "scroll", "scroll_to", "wait_for",
            "extract_text", "extract_table", "get_links",
        ):
            setattr(self, name, AsyncMock())


def _skill(session: _FakeSession | None = None) -> WebBrowserSkill:
    return WebBrowserSkill(session or _FakeSession())


def _request(action: str, arguments: dict) -> ActionRequest:
    return ActionRequest(skill="web", action=action, arguments=arguments, source=ActionSource.voice_wake_word)


# ---- validation -------------------------------------------------------------

async def test_validate_requires_target_for_click_and_find():
    skill = _skill()
    with pytest.raises(SkillValidationError):
        await skill.validate("click", {})
    with pytest.raises(SkillValidationError):
        await skill.validate("find", {"target": "  "})
    await skill.validate("click", {"target": "sign in"})


async def test_validate_navigate_requires_url():
    skill = _skill()
    with pytest.raises(SkillValidationError):
        await skill.validate("navigate", {})
    await skill.validate("navigate", {"url": "https://example.com"})


async def test_validate_type_requires_target_and_text():
    skill = _skill()
    with pytest.raises(SkillValidationError):
        await skill.validate("type", {"target": "search box"})
    await skill.validate("type", {"target": "search box", "text": "hello"})


async def test_validate_scroll_direction_enum():
    skill = _skill()
    with pytest.raises(SkillValidationError):
        await skill.validate("scroll", {"direction": "sideways"})
    await skill.validate("scroll", {"direction": "up"})


async def test_validate_wait_for_timeout_bounds():
    skill = _skill()
    with pytest.raises(SkillValidationError):
        await skill.validate("wait_for", {"target": "x", "timeout": 999})
    await skill.validate("wait_for", {"target": "x", "timeout": 5})


async def test_validate_rejects_unknown_action():
    skill = _skill()
    with pytest.raises(SkillValidationError):
        await skill.validate("teleport", {})


# ---- risk classification ---------------------------------------------------

def test_read_only_actions():
    skill = _skill()
    for action in ("get_context", "list_tabs", "find", "extract_text", "extract_table", "get_links"):
        assert skill.classify_risk(action, {}) == RiskLevel.READ_ONLY


def test_click_elevates_for_state_changing_target():
    skill = _skill()
    assert skill.classify_risk("click", {"target": "the search box"}) == RiskLevel.LOW_RISK
    assert skill.classify_risk("click", {"target": "buy now"}) == RiskLevel.CONFIRM_REQUIRED
    assert skill.classify_risk("click", {"target": "submit order"}) == RiskLevel.CONFIRM_REQUIRED


def test_type_elevates_for_sensitive_target():
    skill = _skill()
    assert skill.classify_risk("type", {"target": "search box"}) == RiskLevel.LOW_RISK
    assert skill.classify_risk("type", {"target": "password field"}) == RiskLevel.CONFIRM_REQUIRED
    assert skill.classify_risk("type", {"target": "name", "is_sensitive": True}) == RiskLevel.CONFIRM_REQUIRED


def test_unknown_action_is_blocked():
    skill = _skill()
    assert skill.classify_risk("delete_everything", {}) == RiskLevel.BLOCKED


# ---- execute(): navigation --------------------------------------------------

async def test_execute_navigate_success():
    session = _FakeSession()
    session.navigate.return_value = {"ok": True, "url": "https://example.com/", "title": "Example", "requested": "https://example.com"}
    result = await _skill(session).execute(_request("navigate", {"url": "https://example.com"}))
    assert result.status == ActionStatus.SUCCEEDED
    assert result.data["outcome"] == "SUCCESS"


async def test_execute_navigate_partial_same_host():
    session = _FakeSession()
    session.navigate.return_value = {"ok": False, "url": "https://example.com/slow", "title": "", "requested": "https://example.com"}
    result = await _skill(session).execute(_request("navigate", {"url": "https://example.com"}))
    assert result.status == ActionStatus.FAILED
    assert result.data["outcome"] == "PARTIAL"


async def test_execute_navigate_not_verified_different_host():
    session = _FakeSession()
    session.navigate.return_value = {"ok": False, "url": "", "title": "", "requested": "https://example.com"}
    result = await _skill(session).execute(_request("navigate", {"url": "https://example.com"}))
    assert result.data["outcome"] == "NOT_VERIFIED"


async def test_execute_back_no_history():
    session = _FakeSession()
    session.back.return_value = {"ok": False, "url": "https://example.com", "title": "Example", "changed": False}
    result = await _skill(session).execute(_request("back", {}))
    assert result.status == ActionStatus.FAILED
    assert result.data["outcome"] == "NOT_FOUND"


# ---- execute(): element resolution ------------------------------------------

async def test_execute_click_success():
    session = _FakeSession()
    session.click.return_value = {"ok": True, "matched": {"tag": "button", "text": "Sign in"}}
    result = await _skill(session).execute(_request("click", {"target": "sign in"}))
    assert result.status == ActionStatus.SUCCEEDED
    assert "Sign in" in result.summary


async def test_execute_click_not_found():
    session = _FakeSession()
    session.click.return_value = {"ok": False, "reason": "NOT_FOUND", "candidates": []}
    result = await _skill(session).execute(_request("click", {"target": "nope"}))
    assert result.status == ActionStatus.FAILED
    assert result.data["outcome"] == "NOT_FOUND"
    assert "not found" in result.error.lower() or "not found" in result.summary.lower()


async def test_execute_click_ambiguous():
    session = _FakeSession()
    session.click.return_value = {"ok": False, "reason": "AMBIGUOUS", "candidates": [{"text": "A"}, {"text": "B"}]}
    result = await _skill(session).execute(_request("click", {"target": "buy now"}))
    assert result.data["outcome"] == "AMBIGUOUS"
    assert "ambiguous" in result.summary.lower()


async def test_execute_click_script_error_raises():
    session = _FakeSession()
    session.click.return_value = {"ok": False, "reason": "SCRIPT_ERROR", "error": "boom"}
    with pytest.raises(SkillExecutionError):
        await _skill(session).execute(_request("click", {"target": "x"}))


async def test_execute_type_redacts_sensitive_value():
    session = _FakeSession()
    session.type_text.return_value = {"ok": True, "matched": {"tag": "input", "text": "secret123"}, "value_after": "secret123"}
    result = await _skill(session).execute(_request("type", {"target": "password field", "text": "secret123"}))
    assert result.status == ActionStatus.SUCCEEDED
    assert result.data["matched"]["text"] == "[REDACTED]"
    assert result.data["value_after"] == "[REDACTED]"


# ---- execute(): extraction ---------------------------------------------------

async def test_execute_extract_text():
    session = _FakeSession()
    session.extract_text.return_value = {"ok": True, "mode": "summary", "text": "hello world"}
    result = await _skill(session).execute(_request("extract_text", {"mode": "summary"}))
    assert result.status == ActionStatus.SUCCEEDED
    assert result.data["text"] == "hello world"


async def test_execute_extract_table_not_found():
    session = _FakeSession()
    session.extract_table.return_value = {"ok": False, "reason": "NOT_FOUND"}
    result = await _skill(session).execute(_request("extract_table", {}))
    assert result.status == ActionStatus.FAILED


# ---- execute(): launch/transport failures surface honestly -----------------

async def test_execute_surfaces_launch_failure():
    session = _FakeSession()
    session.get_context.side_effect = BrowserLaunchError("no chrome found")
    result = await _skill(session).execute(_request("get_context", {}))
    assert result.status == ActionStatus.FAILED
    assert "no chrome found" in result.summary


async def test_execute_surfaces_cdp_error():
    session = _FakeSession()
    session.navigate.side_effect = CDPError("socket closed")
    result = await _skill(session).execute(_request("navigate", {"url": "https://example.com"}))
    assert result.status == ActionStatus.FAILED
    assert "socket closed" in result.summary
