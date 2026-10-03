"""AssistantTurnController._chart_analysis (the "analyze this chart" path).

Live testing found this path returning a non-answer ("I don't have a fresh
chart observation yet... ask me again") that depended entirely on the
background chart watcher and never actually delivered an analysis. The fix
makes it capture a fresh frame itself and analyze synchronously, in the same
turn, or report the exact failure -- see turn_controller.py's _chart_analysis
docstring. These tests exercise that path end to end with fake collaborators.
"""
from __future__ import annotations

import base64

import aiosqlite
import pytest
from PIL import Image
import io

from app.action_contracts import ActionRequest, ActionResult, ActionStatus, RiskLevel
from app.config import Settings
from assistant.chart_analysis import ChartAnalysisService
from assistant.conversation_store import ConversationStore
from assistant.provider import AssistantProvider, AssistantReply, AssistantRequest
from assistant.turn_controller import AssistantTurnController, TurnStatus
from storage.migrator import run_migrations

_STRUCTURED_JSON = """{
  "instrument": "EURUSD",
  "timeframe": "4H",
  "current_price_context": "1.0920, mid-range",
  "supply_zone": "1.0950",
  "demand_zone": "1.0870",
  "recent_price_sequence": "Pulled back, now consolidating.",
  "at_meaningful_location": true,
  "market_context": "Price is consolidating below a prior swing high.",
  "key_levels": ["1.0950 resistance", "1.0870 support"],
  "possible_setup": "A break and retest of 1.0950 could favor continuation long.",
  "invalidation": "a daily close below 1.0870",
  "risk_notes": "Low sample of visible candles."
}"""


class _NeverUsed:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected dependency use: {name}")


class _FakeChartProvider(AssistantProvider):
    name = "fake_chart"

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        return AssistantReply(text=_STRUCTURED_JSON, provider=self.name)


class _FakeActionRuntime:
    def __init__(self, result: ActionResult) -> None:
        self.result = result
        self.requests: list[ActionRequest] = []

    async def submit(self, request: ActionRequest) -> ActionResult:
        self.requests.append(request)
        return self.result


def _bmp_data_uri(width: int = 4, height: int = 4) -> str:
    """Matches the real shape lib.rs's capture_active_window returns --
    `data:image/bmp;base64,<...>`, NOT raw base64. A regression once slipped
    past this suite because an earlier version of this helper returned raw
    base64, which turn_controller._chart_analysis then failed to decode
    only against the real native build, not these tests -- see the
    partition(",") handling it now shares with app/routers/assistant.py."""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(10, 20, 30)).save(buf, format="BMP")
    return "data:image/bmp;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _capture_result(*, executable: str, window_title: str, image_b64: str | None, error: str | None = None,
                     is_secure_desktop: bool = False, status: ActionStatus = ActionStatus.SUCCEEDED) -> ActionResult:
    return ActionResult(
        request_id=__import__("uuid").uuid4(),
        status=status,
        risk_level=RiskLevel.READ_ONLY,
        summary="captured" if status is ActionStatus.SUCCEEDED else "capture failed",
        data={
            "executable": executable,
            "window_title": window_title,
            "image_data_base64": image_b64,
            "image_format": "image/bmp",
            "is_secure_desktop": is_secure_desktop,
            "error": error,
        },
        error=error,
    )


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "chart_turn.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


def _make_controller(conn, *, action_runtime, chart_analysis_service) -> AssistantTurnController:
    return AssistantTurnController(
        settings=Settings(database_url="sqlite:///:memory:"),
        provider=_NeverUsed(),
        assistant_router=_NeverUsed(),
        orchestrator=_NeverUsed(),
        action_runtime=action_runtime,
        conversation_store=ConversationStore(conn),
        hot_chart_state_store=_NeverUsed(),
        chart_analysis_service=chart_analysis_service,
    )


async def test_analyze_this_chart_captures_fresh_and_returns_real_analysis_same_turn(conn):
    runtime = _FakeActionRuntime(_capture_result(
        executable="tradingview.exe", window_title="EURUSD - TradingView", image_b64=_bmp_data_uri(),
    ))
    controller = _make_controller(
        conn, action_runtime=runtime, chart_analysis_service=ChartAnalysisService(_FakeChartProvider())
    )

    response = await controller.execute_text("analyze this chart")

    assert response.status is TurnStatus.COMPLETED
    assert "EURUSD" in response.display_text
    assert runtime.requests[0].skill == "windows_app"
    assert runtime.requests[0].action == "capture_active_window"


async def test_analyze_this_chart_refuses_a_non_chart_window_instead_of_analyzing_it(conn):
    """Acceptance scenario: a stale TradingView watcher exists but the user
    switched to Chrome -- capturing Chrome must report NOT_A_CHART-shaped
    truth, never send Chrome's screenshot to the chart vision model."""
    runtime = _FakeActionRuntime(_capture_result(
        executable="chrome.exe", window_title="New Tab - Google Chrome", image_b64=_bmp_data_uri(),
    ))
    provider = _FakeChartProvider()
    controller = _make_controller(
        conn, action_runtime=runtime, chart_analysis_service=ChartAnalysisService(provider)
    )

    response = await controller.execute_text("analyze this chart")

    assert response.status is TurnStatus.COMPLETED
    assert "doesn't look like a supported chart" in response.display_text
    assert "Chrome" in response.display_text


async def test_analyze_this_chart_reports_capture_failure_honestly(conn):
    runtime = _FakeActionRuntime(_capture_result(
        executable="", window_title="", image_b64=None, status=ActionStatus.FAILED,
    ))
    runtime.result.summary = "windows_app.capture_active_window() requires a connected native shell"
    controller = _make_controller(
        conn, action_runtime=runtime, chart_analysis_service=ChartAnalysisService(_FakeChartProvider())
    )

    response = await controller.execute_text("analyze this chart")

    assert response.status is TurnStatus.COMPLETED
    assert "native shell" in response.display_text


async def test_analyze_this_chart_refuses_secure_desktop_capture(conn):
    runtime = _FakeActionRuntime(_capture_result(
        executable="", window_title="", image_b64=None, is_secure_desktop=True,
    ))
    controller = _make_controller(
        conn, action_runtime=runtime, chart_analysis_service=ChartAnalysisService(_FakeChartProvider())
    )

    response = await controller.execute_text("analyze this chart")

    assert response.status is TurnStatus.COMPLETED
    assert "secure" in response.display_text.lower()
