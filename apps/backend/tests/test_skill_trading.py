from __future__ import annotations

import aiosqlite
import pytest

from app.action_contracts import (
    ActionRequest,
    ActionSource,
    ActionStatus,
    RiskLevel,
    SkillExecutionError,
    SkillValidationError,
)
from events.service import EventService
from memory.service import MemoryService
from skills.trading import TradingSkill
from storage.migrator import run_migrations
from trading.context import TradingContextBuilder
from trading.provider import NullStrategyProvider


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "trading_skill_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


@pytest.fixture
async def memory(conn, tmp_path):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    return MemoryService(conn, vault_path=str(vault_dir), sqlite_vec_enabled=False)


@pytest.fixture
def context_builder(conn):
    return TradingContextBuilder(EventService(conn), NullStrategyProvider())


@pytest.fixture
def skill(memory, context_builder):
    return TradingSkill(memory_service=memory, context_builder=context_builder, bridge=None)


def _request(action: str, arguments: dict | None = None) -> ActionRequest:
    return ActionRequest(
        skill="trading", action=action, arguments=arguments or {}, source=ActionSource.deterministic
    )


def test_classify_risk(skill):
    assert skill.classify_risk("capture_chart", {}) == RiskLevel.READ_ONLY
    assert skill.classify_risk("get_trading_context", {}) == RiskLevel.READ_ONLY
    assert skill.classify_risk("save_trading_observation", {}) == RiskLevel.LOW_RISK
    assert skill.classify_risk("open_tradingview", {}) == RiskLevel.LOW_RISK
    assert skill.classify_risk("delete_everything", {}) == RiskLevel.BLOCKED


async def test_validate_explain_setup_requires_symbol(skill):
    with pytest.raises(SkillValidationError):
        await skill.validate("explain_setup", {})
    await skill.validate("explain_setup", {"symbol": "ES"})


async def test_validate_save_observation_requires_text(skill):
    with pytest.raises(SkillValidationError):
        await skill.validate("save_trading_observation", {})
    await skill.validate("save_trading_observation", {"text": "breakout forming"})


async def test_validate_rejects_unknown_action(skill):
    with pytest.raises(SkillValidationError):
        await skill.validate("nonexistent", {})


async def test_capture_chart_fails_closed_without_bridge(skill):
    # No FrontendCommandBridge wired -- raises rather than fabricating a
    # result, same convention as windows_app's capture actions; the action
    # runtime is what converts this into a FAILED ActionResult.
    with pytest.raises(SkillExecutionError, match="connected native shell"):
        await skill.execute(_request("capture_chart"))


async def test_get_trading_context_reports_not_configured_by_default(skill):
    result = await skill.execute(_request("get_trading_context"))
    assert result.status == ActionStatus.SUCCEEDED
    assert result.data["strategy_status"] == "NOT_CONFIGURED"
    assert result.data["active_setups"] == []


async def test_explain_setup_reports_no_active_setup(skill):
    result = await skill.execute(_request("explain_setup", {"symbol": "ES"}))
    assert result.status == ActionStatus.SUCCEEDED
    assert result.data["found"] is False


async def test_save_and_search_trading_observation_round_trip(skill):
    save_result = await skill.execute(
        _request("save_trading_observation", {"text": "gold broke structure", "symbol": "XAUUSD"})
    )
    assert save_result.status == ActionStatus.SUCCEEDED
    note_id = save_result.data["note_id"]
    assert note_id

    search_result = await skill.execute(
        _request("search_trading_memory", {"query": "structure"})
    )
    assert search_result.status == ActionStatus.SUCCEEDED
    assert len(search_result.data["results"]) == 1
    assert search_result.data["results"][0]["source"] == "trading_observation"


async def test_open_tradingview_uses_webbrowser(skill, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr("skills.trading.webbrowser.open", lambda url: calls.append(url) or True)
    result = await skill.execute(_request("open_tradingview"))
    assert result.status == ActionStatus.SUCCEEDED
    assert calls == ["https://www.tradingview.com/chart/"]


async def test_focus_tradingview_fails_closed_when_not_running(skill):
    result = await skill.execute(_request("focus_tradingview"))
    assert result.status == ActionStatus.FAILED


# ---- mission: FINAL INFRASTRUCTURE MISSION section 19 --------------------
# analyze_active_chart must refuse a non-chart foreground window honestly,
# reusing assistant/chart_capture.py's CHART_WINDOW_RE guard, instead of
# fabricating a structured analysis of whatever happens to be in front.

class _FakeBridge:
    def __init__(self, capture_data: dict):
        self._data = capture_data
        self.calls = 0

    async def dispatch(self, request_id, skill_name, action, args, *, timeout):
        self.calls += 1
        return self._data


class _FakeChartAnalysisService:
    def __init__(self):
        self.calls = 0

    async def analyze(self, **kwargs):
        self.calls += 1
        from assistant.chart_analysis import ChartAnalysisResult

        return ChartAnalysisResult(
            instrument="EURUSD", timeframe="15m", market_context="Quiet.", key_levels=[],
            possible_setup=None, invalidation=None, risk_notes="none", provider="fake",
            raw_text="{}", structured=True,
        )


def _bmp_data_uri() -> str:
    import base64
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color=(1, 2, 3)).save(buf, format="BMP")
    return "data:image/bmp;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


async def test_analyze_active_chart_refuses_a_non_chart_foreground_window(memory, context_builder):
    bridge = _FakeBridge({
        "executable": "chrome.exe", "window_title": "ChatGPT - Google Chrome",
        "image_data_base64": _bmp_data_uri(), "image_format": "image/bmp",
        "is_secure_desktop": False, "error": None, "active_context": None,
    })
    chart_analysis = _FakeChartAnalysisService()
    skill = TradingSkill(memory_service=memory, context_builder=context_builder, bridge=bridge,
                         chart_analysis_service=chart_analysis)

    result = await skill.execute(_request("analyze_active_chart"))

    assert result.status == ActionStatus.FAILED
    assert result.error == "not_a_chart"
    assert "ChatGPT" in result.summary or "Chrome" in result.summary
    assert chart_analysis.calls == 0  # never fabricated an analysis of the wrong window


async def test_analyze_active_chart_proceeds_for_a_real_chart_window(memory, context_builder):
    bridge = _FakeBridge({
        "executable": "TradingView.exe", "window_title": "EURUSD - TradingView",
        "image_data_base64": _bmp_data_uri(), "image_format": "image/bmp",
        "is_secure_desktop": False, "error": None, "active_context": None,
    })
    chart_analysis = _FakeChartAnalysisService()
    skill = TradingSkill(memory_service=memory, context_builder=context_builder, bridge=bridge,
                         chart_analysis_service=chart_analysis)

    result = await skill.execute(_request("analyze_active_chart"))

    assert result.status == ActionStatus.SUCCEEDED
    assert chart_analysis.calls == 1
