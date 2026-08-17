from __future__ import annotations

from unittest.mock import patch

import pytest

from app.action_contracts import (
    ActionRequest,
    ActionSource,
    ActionStatus,
    RiskLevel,
    SkillValidationError,
)
from skills.browser import BrowserSkill, validate_http_url


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
