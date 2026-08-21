from __future__ import annotations

import io

import aiosqlite
import pytest
from PIL import Image

from assistant.chart_analysis import ChartAnalysisService
from assistant.chart_watch import ChartWatchService
from assistant.hot_chart_state import ChartIdentity, HotChartState
from assistant.hot_chart_state_store import HotChartStateStore
from assistant.provider import AssistantProvider, AssistantReply, AssistantRequest
from storage.migrator import run_migrations

_STRUCTURED_JSON = """{
  "instrument": "XAUUSD",
  "timeframe": "15M",
  "current_price_context": "Near range highs",
  "supply_zone": "4550",
  "demand_zone": "4500",
  "recent_price_sequence": "V-shaped recovery",
  "at_meaningful_location": true,
  "market_context": "Consolidating below resistance.",
  "key_levels": ["Resistance: 4550", "Support: 4500"],
  "possible_setup": null,
  "invalidation": "Break below 4500",
  "risk_notes": "Macro/event risk has not been checked in this chart-only analysis."
}"""


class _FakeProvider(AssistantProvider):
    name = "fake"

    def __init__(self, reply_text: str = _STRUCTURED_JSON, *, raise_error: bool = False) -> None:
        self.reply_text = reply_text
        self.raise_error = raise_error
        self.call_count = 0

    async def respond(self, request: AssistantRequest) -> AssistantReply:
        self.call_count += 1
        if self.raise_error:
            raise RuntimeError("provider exploded")
        return AssistantReply(text=self.reply_text, provider=self.name)


def _bmp_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color=(10, 20, 30)).save(buf, format="BMP")
    return buf.getvalue()


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "chart_watch_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


async def test_handle_frame_refreshes_and_persists_hot_state(conn):
    provider = _FakeProvider()
    service = ChartWatchService(ChartAnalysisService(provider), HotChartStateStore(conn))

    outcome = await service.handle_frame(
        chart_window_id="hwnd-1", image_bytes=_bmp_bytes(), image_format="image/bmp", trigger_reason="visual_change"
    )

    assert outcome.action == "refreshed"
    assert outcome.identity is not None
    assert outcome.identity.symbol == "XAUUSD"
    assert outcome.identity.timeframe == "15M"
    assert provider.call_count == 1

    stored = await HotChartStateStore(conn).get(outcome.identity)
    assert stored is not None
    assert stored.analysis.instrument == "XAUUSD"


async def test_handle_frame_respects_min_vision_cooldown(conn):
    provider = _FakeProvider()
    service = ChartWatchService(
        ChartAnalysisService(provider), HotChartStateStore(conn), min_vision_cooldown_seconds=1000.0
    )

    first = await service.handle_frame(
        chart_window_id="hwnd-1", image_bytes=_bmp_bytes(), image_format="image/bmp", trigger_reason="visual_change"
    )
    second = await service.handle_frame(
        chart_window_id="hwnd-1", image_bytes=_bmp_bytes(), image_format="image/bmp", trigger_reason="visual_change"
    )

    assert first.action == "refreshed"
    assert second.action == "skipped_cooldown"
    assert provider.call_count == 1


async def test_handle_frame_cooldown_is_scoped_per_window(conn):
    provider = _FakeProvider()
    service = ChartWatchService(
        ChartAnalysisService(provider), HotChartStateStore(conn), min_vision_cooldown_seconds=1000.0
    )

    await service.handle_frame(
        chart_window_id="hwnd-1", image_bytes=_bmp_bytes(), image_format="image/bmp", trigger_reason="visual_change"
    )
    other_window = await service.handle_frame(
        chart_window_id="hwnd-2", image_bytes=_bmp_bytes(), image_format="image/bmp", trigger_reason="visual_change"
    )

    assert other_window.action == "refreshed"
    assert provider.call_count == 2


async def test_handle_frame_skips_when_existing_state_is_still_fresh(conn):
    store = HotChartStateStore(conn)
    provider = _FakeProvider()
    # min_vision_cooldown_seconds=0 isolates this test to the freshness
    # check specifically, not the cooldown check above.
    service = ChartWatchService(ChartAnalysisService(provider), store, min_vision_cooldown_seconds=0.0)

    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    fresh_identity = ChartIdentity(chart_window_id="hwnd-1", symbol="XAUUSD", timeframe="15M")
    await store.upsert(_hot_state_with_result(fresh_identity, now))

    outcome = await service.handle_frame(
        chart_window_id="hwnd-1", image_bytes=_bmp_bytes(), image_format="image/bmp", trigger_reason="visual_change"
    )

    assert outcome.action == "skipped_fresh"
    assert provider.call_count == 0


async def test_handle_frame_refreshes_when_existing_state_is_stale(conn):
    store = HotChartStateStore(conn)
    provider = _FakeProvider()
    service = ChartWatchService(ChartAnalysisService(provider), store, min_vision_cooldown_seconds=0.0)

    from datetime import UTC, datetime, timedelta

    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    stale_identity = ChartIdentity(chart_window_id="hwnd-1", symbol="XAUUSD", timeframe="5M")
    await store.upsert(_hot_state_with_result(stale_identity, old))

    outcome = await service.handle_frame(
        chart_window_id="hwnd-1", image_bytes=_bmp_bytes(), image_format="image/bmp", trigger_reason="visual_change"
    )

    assert outcome.action == "refreshed"
    assert provider.call_count == 1


async def test_handle_frame_surfaces_provider_error_without_persisting(conn):
    store = HotChartStateStore(conn)
    provider = _FakeProvider(raise_error=True)
    service = ChartWatchService(ChartAnalysisService(provider), store, min_vision_cooldown_seconds=0.0)

    outcome = await service.handle_frame(
        chart_window_id="hwnd-1", image_bytes=_bmp_bytes(), image_format="image/bmp", trigger_reason="visual_change"
    )

    assert outcome.action == "error"
    assert outcome.error is not None
    assert "provider exploded" in outcome.error
    assert await store.get_latest_for_window("hwnd-1") is None


def _hot_state_with_result(identity: ChartIdentity, analyzed_at: str) -> HotChartState:
    from assistant.chart_analysis import ChartAnalysisResult

    result = ChartAnalysisResult(
        instrument=identity.symbol,
        timeframe=identity.timeframe,
        market_context="test",
        key_levels=[],
        possible_setup=None,
        invalidation=None,
        risk_notes="Macro/event risk has not been checked in this chart-only analysis.",
        provider="claude_code",
        raw_text="{}",
        structured=True,
    )
    return HotChartState(
        identity=identity,
        analysis=result,
        screenshot_hash="prior",
        source="vision",
        observed_at=analyzed_at,
        analyzed_at=analyzed_at,
    )
