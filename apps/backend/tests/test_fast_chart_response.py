from __future__ import annotations

import io
import time
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
from PIL import Image

from assistant.chart_analysis import ChartAnalysisResult
from assistant.fast_chart_response import try_fast_response
from assistant.hot_chart_state import ChartIdentity, HotChartState
from assistant.hot_chart_state_store import HotChartStateStore
from assistant.perceptual_hash import average_hash_hex
from storage.migrator import run_migrations


@pytest.fixture
async def conn(tmp_path):
    db_path = tmp_path / "fast_response_test.db"
    run_migrations(db_path)
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    yield connection
    await connection.close()


def _structured_bmp_bytes(*, flipped: bool = False) -> bytes:
    img = Image.new("RGB", (32, 32))
    pixels = img.load()
    for y in range(32):
        for x in range(32):
            dark = (x < 16) != (y < 16)
            if flipped:
                dark = not dark
            pixels[x, y] = (10, 10, 10) if dark else (230, 230, 230)
    buf = io.BytesIO()
    img.save(buf, format="BMP")
    return buf.getvalue()


def _result() -> ChartAnalysisResult:
    return ChartAnalysisResult(
        instrument="XAUUSD",
        timeframe="15M",
        market_context="Ranging near resistance.",
        key_levels=["Resistance: 4550", "Support: 4500"],
        possible_setup=None,
        invalidation="Break below 4500",
        risk_notes="Macro/event risk has not been checked in this chart-only analysis.",
        provider="claude_code",
        raw_text="{}",
        structured=True,
    )


async def _seed(store, *, chart_window_id, symbol, timeframe, analyzed_at, screenshot_hash):
    state = HotChartState(
        identity=ChartIdentity(chart_window_id=chart_window_id, symbol=symbol, timeframe=timeframe),
        analysis=_result(),
        screenshot_hash=screenshot_hash,
        source="vision",
        observed_at=analyzed_at,
        analyzed_at=analyzed_at,
    )
    await store.upsert(state)


async def test_returns_none_without_a_window_id(conn):
    store = HotChartStateStore(conn)
    outcome = await try_fast_response(
        window_id=None, image_bytes=_structured_bmp_bytes(), hot_state_store=store, t0=time.monotonic()
    )
    assert outcome is None


async def test_returns_none_when_nothing_is_stored_for_the_window(conn):
    store = HotChartStateStore(conn)
    outcome = await try_fast_response(
        window_id="hwnd-1", image_bytes=_structured_bmp_bytes(), hot_state_store=store, t0=time.monotonic()
    )
    assert outcome is None


async def test_returns_a_response_when_hot_and_content_matches(conn):
    store = HotChartStateStore(conn)
    frame = _structured_bmp_bytes()
    now = datetime.now(UTC).isoformat()
    await _seed(
        store,
        chart_window_id="hwnd-1",
        symbol="XAUUSD",
        timeframe="15M",
        analyzed_at=now,
        screenshot_hash=average_hash_hex(Image.open(io.BytesIO(frame))),
    )

    outcome = await try_fast_response(
        window_id="hwnd-1", image_bytes=frame, hot_state_store=store, t0=time.monotonic()
    )

    assert outcome is not None
    assert outcome.result["instrument"] == "XAUUSD"
    assert outcome.timing["warm_path"] is True


async def test_returns_none_when_state_is_only_warm_not_hot(conn):
    # 15M timeframe: HOT <=60s, WARM <=180s -- 90s old is WARM, not HOT.
    store = HotChartStateStore(conn)
    frame = _structured_bmp_bytes()
    old = (datetime.now(UTC) - timedelta(seconds=90)).isoformat()
    await _seed(
        store,
        chart_window_id="hwnd-1",
        symbol="XAUUSD",
        timeframe="15M",
        analyzed_at=old,
        screenshot_hash=average_hash_hex(Image.open(io.BytesIO(frame))),
    )

    outcome = await try_fast_response(
        window_id="hwnd-1", image_bytes=frame, hot_state_store=store, t0=time.monotonic()
    )
    assert outcome is None


async def test_returns_none_when_state_is_stale(conn):
    store = HotChartStateStore(conn)
    frame = _structured_bmp_bytes()
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    await _seed(
        store,
        chart_window_id="hwnd-1",
        symbol="XAUUSD",
        timeframe="15M",
        analyzed_at=old,
        screenshot_hash=average_hash_hex(Image.open(io.BytesIO(frame))),
    )

    outcome = await try_fast_response(
        window_id="hwnd-1", image_bytes=frame, hot_state_store=store, t0=time.monotonic()
    )
    assert outcome is None


async def test_returns_none_when_content_has_drifted_even_if_hot(conn):
    # Regression for Part 19/26: a HOT-by-age row for a visually different
    # frame (e.g. a symbol switch that just happened) must not be served.
    store = HotChartStateStore(conn)
    old_frame = _structured_bmp_bytes(flipped=False)
    new_frame = _structured_bmp_bytes(flipped=True)
    now = datetime.now(UTC).isoformat()
    await _seed(
        store,
        chart_window_id="hwnd-1",
        symbol="XAUUSD",
        timeframe="15M",
        analyzed_at=now,
        screenshot_hash=average_hash_hex(Image.open(io.BytesIO(old_frame))),
    )

    outcome = await try_fast_response(
        window_id="hwnd-1", image_bytes=new_frame, hot_state_store=store, t0=time.monotonic()
    )
    assert outcome is None


async def test_returns_none_for_a_different_window_id(conn):
    store = HotChartStateStore(conn)
    frame = _structured_bmp_bytes()
    now = datetime.now(UTC).isoformat()
    await _seed(
        store,
        chart_window_id="hwnd-1",
        symbol="XAUUSD",
        timeframe="15M",
        analyzed_at=now,
        screenshot_hash=average_hash_hex(Image.open(io.BytesIO(frame))),
    )

    outcome = await try_fast_response(
        window_id="hwnd-2", image_bytes=frame, hot_state_store=store, t0=time.monotonic()
    )
    assert outcome is None
