from __future__ import annotations

from unittest.mock import patch

import pytest

from actions.frontend_bridge import FrontendBridgeError
from app.action_contracts import (
    ActionRequest,
    ActionSource,
    ActionStatus,
    RiskLevel,
    SkillExecutionError,
    SkillValidationError,
)
from skills.browser import BrowserSkill, validate_http_url


class _FakeBridge:
    """Stands in for FrontendCommandBridge -- returns a canned payload or
    raises, so tests can assert BrowserSkill never fabricates SUCCEEDED
    without a real bridge report."""

    def __init__(self, *, payload: dict | None = None, error: str | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[tuple] = []

    async def dispatch(self, request_id, skill, action, arguments, *, timeout):
        self.calls.append((request_id, skill, action, arguments, timeout))
        if self.error is not None:
            raise FrontendBridgeError(self.error)
        return self.payload or {}


def _request(action: str, arguments: dict) -> ActionRequest:
    return ActionRequest(
        skill="browser", action=action, arguments=arguments, source=ActionSource.hud
    )


def test_classify_risk():
    skill = BrowserSkill()
    assert skill.classify_risk("open_url", {}) == RiskLevel.LOW_RISK
    assert skill.classify_risk("search", {}) == RiskLevel.LOW_RISK
    assert skill.classify_risk("navigate_and_click", {}) == RiskLevel.BLOCKED


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://example.com/path?query=1",
        "https://example.com:8080/",
    ],
)
def test_validate_http_url_accepts_valid_http_https(url):
    assert validate_http_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/System32/cmd.exe",
        "javascript:alert(1)",
        "ftp://example.com",
        "data:text/html,<script>alert(1)</script>",
        "",
        "not a url",
        "example.com",
    ],
)
def test_validate_http_url_rejects_disallowed_schemes(url):
    with pytest.raises(SkillValidationError):
        validate_http_url(url)


async def test_validate_open_url_rejects_bad_scheme():
    skill = BrowserSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("open_url", {"url": "javascript:alert(1)"})


async def test_validate_search_requires_query():
    skill = BrowserSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("search", {})
    await skill.validate("search", {"query": "python"})


async def test_validate_rejects_unknown_action():
    skill = BrowserSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("scrape", {})


async def test_execute_open_url_calls_webbrowser_open():
    skill = BrowserSkill()
    with patch("skills.browser.webbrowser.open", return_value=True) as mock_open:
        result = await skill.execute(_request("open_url", {"url": "https://example.com"}))

    mock_open.assert_called_once_with("https://example.com")
    assert result.status == ActionStatus.SUCCEEDED
    assert result.data["url"] == "https://example.com"


async def test_execute_search_builds_search_url_and_opens_it():
    skill = BrowserSkill()
    with patch("skills.browser.webbrowser.open", return_value=True) as mock_open:
        result = await skill.execute(_request("search", {"query": "TARS assistant"}))

    assert result.status == ActionStatus.SUCCEEDED
    (opened_url,), _ = mock_open.call_args
    assert opened_url.startswith("https://www.google.com/search?q=")
    assert "TARS" in opened_url or "TARS%20assistant" in opened_url or "TARS+assistant" in opened_url


async def test_execute_reports_failure_when_no_browser_available():
    skill = BrowserSkill()
    with patch("skills.browser.webbrowser.open", return_value=False):
        result = await skill.execute(_request("open_url", {"url": "https://example.com"}))

    assert result.status == ActionStatus.FAILED
    assert result.error is not None


# -- Wave 2B embedded-DOM actions ---------------------------------------


def test_classify_risk_dom_actions():
    skill = BrowserSkill()
    assert skill.classify_risk("navigate", {"url": "https://x.test"}) == RiskLevel.LOW_RISK
    assert skill.classify_risk("back", {}) == RiskLevel.LOW_RISK
    assert skill.classify_risk("forward", {}) == RiskLevel.LOW_RISK
    assert skill.classify_risk("inspect_dom", {}) == RiskLevel.READ_ONLY
    assert skill.classify_risk("read_text", {}) == RiskLevel.READ_ONLY
    assert skill.classify_risk("scroll", {}) == RiskLevel.READ_ONLY


def test_classify_risk_click_elevates_for_state_changing_target():
    skill = BrowserSkill()
    assert skill.classify_risk("click", {"target": "Add to cart"}) == RiskLevel.LOW_RISK
    assert skill.classify_risk("click", {"target": "Submit order"}) == RiskLevel.CONFIRM_REQUIRED
    assert skill.classify_risk("click", {"target": "Delete account"}) == RiskLevel.CONFIRM_REQUIRED
    assert skill.classify_risk("click", {"selector": "#buy-now"}) == RiskLevel.CONFIRM_REQUIRED


def test_classify_risk_type_elevates_for_sensitive_field():
    skill = BrowserSkill()
    assert skill.classify_risk("type", {"selector": "#username", "text": "a"}) == RiskLevel.LOW_RISK
    assert (
        skill.classify_risk("type", {"selector": "#password", "text": "hunter2"})
        == RiskLevel.CONFIRM_REQUIRED
    )
    assert (
        skill.classify_risk("type", {"selector": "#note", "text": "x", "is_sensitive": True})
        == RiskLevel.CONFIRM_REQUIRED
    )


async def test_validate_navigate_rejects_bad_scheme():
    skill = BrowserSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("navigate", {"url": "javascript:alert(1)"})


async def test_validate_click_requires_target():
    skill = BrowserSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("click", {})
    await skill.validate("click", {"target": "Go"})


async def test_validate_type_requires_selector_and_text():
    skill = BrowserSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("type", {"selector": "#x"})
    await skill.validate("type", {"selector": "#x", "text": "hello"})


async def test_execute_dom_action_without_bridge_refuses_rather_than_fabricates():
    skill = BrowserSkill()  # no bridge wired
    with pytest.raises(SkillExecutionError):
        await skill.execute(_request("inspect_dom", {}))


async def test_execute_dom_action_dispatches_through_bridge_and_returns_real_data():
    bridge = _FakeBridge(payload={"summary": "Inspected DOM", "elements_count": 3})
    skill = BrowserSkill(bridge=bridge)
    result = await skill.execute(_request("inspect_dom", {}))

    assert result.status == ActionStatus.SUCCEEDED
    assert result.data["elements_count"] == 3
    assert len(bridge.calls) == 1
    request_id, skill_name, action, arguments, timeout = bridge.calls[0]
    assert skill_name == "browser"
    assert action == "inspect_dom"


async def test_execute_dom_action_surfaces_bridge_failure_as_failed_never_succeeded():
    bridge = _FakeBridge(error="Element '#missing' not found")
    skill = BrowserSkill(bridge=bridge)
    result = await skill.execute(_request("click", {"target": "#missing"}))

    assert result.status == ActionStatus.FAILED
    assert "not found" in result.error
