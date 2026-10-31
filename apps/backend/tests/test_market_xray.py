"""Unit tests for the Market X-Ray engine.

These test the properties that make the dashboard trustworthy rather than
merely functional: that ATR matches a hand-computed Wilder series, that no
calculation can see a bar it should not, that order blocks only form when all
three published rules hold, that identity is stable so restarts cannot
duplicate history, and that recorded history can never be labelled LIVE.

Everything here runs on synthetic candles built in-process, so the suite needs
no candle store and no network.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from market import clock, orderblocks, universe
from market.indicators import (
    normalized_return,
    swing_pivots,
    true_range,
    volatility_regime,
    wilder_atr,
)
from market.store import Candle


def candle(ts: datetime, o: float, h: float, low: float, c: float, spread: float = 0.0001) -> Candle:
    return Candle(ts=ts, open=o, high=h, low=low, close=c, spread=spread, volume=1.0)


def series(values: list[tuple[float, float, float, float]], *, start: datetime | None = None,
           minutes: int = 60) -> list[Candle]:
    base = start or datetime(2026, 1, 1, tzinfo=UTC)
    return [
        candle(base + timedelta(minutes=minutes * i), o, h, low, c)
        for i, (o, h, low, c) in enumerate(values)
    ]


# --------------------------------------------------------------------------
# indicators
# --------------------------------------------------------------------------
class TestATR:
    def test_true_range_takes_the_widest_of_the_three_measures(self):
        c = candle(datetime(2026, 1, 1, tzinfo=UTC), 10.0, 12.0, 9.0, 11.0)
        # Gap up: |high - prev_close| is widest.
        assert true_range(5.0, c) == pytest.approx(7.0)
        # Gap down: |low - prev_close| is widest.
        assert true_range(20.0, c) == pytest.approx(11.0)
        # Inside the previous close: plain high-low wins.
        assert true_range(10.5, c) == pytest.approx(3.0)

    def test_atr_is_undefined_until_a_full_period_of_true_ranges_exists(self):
        candles = series([(10, 11, 9, 10)] * 10)
        atr = wilder_atr(candles, period=14)
        assert atr == [None] * 10

    def test_atr_matches_a_hand_computed_wilder_series(self):
        # 20 bars with a constant 2.0 true range, then one 10.0 range bar.
        rows = [(10.0, 11.0, 9.0, 10.0)] * 20
        rows.append((10.0, 15.0, 5.0, 10.0))
        candles = series(rows)
        atr = wilder_atr(candles, period=14)

        # Seed is the mean of the first 14 true ranges, all 2.0.
        assert atr[14] == pytest.approx(2.0)
        # Still 2.0 while every range stays 2.0.
        assert atr[19] == pytest.approx(2.0)
        # Final bar: (2.0 * 13 + 10.0) / 14
        assert atr[20] == pytest.approx((2.0 * 13 + 10.0) / 14)

    def test_atr_is_aligned_index_for_index_with_its_candles(self):
        candles = series([(10, 11, 9, 10)] * 30)
        assert len(wilder_atr(candles, period=14)) == len(candles)


class TestNormalizedReturn:
    def test_returns_none_without_enough_history(self):
        assert normalized_return([1.0, 1.1], window=12) is None

    def test_zero_volatility_yields_no_value_rather_than_infinity(self):
        # A perfectly flat series has no volatility to normalise by.
        assert normalized_return([1.0] * 200, window=12) is None

    def test_positive_move_gives_positive_z_and_pct(self):
        closes = [100.0 + (i % 3) * 0.1 for i in range(200)]
        closes[-1] = 108.0
        result = normalized_return(closes, window=12)
        assert result is not None
        assert result.z > 0
        assert result.pct_change > 0

    def test_z_is_clamped_so_one_outlier_cannot_dominate_a_shared_axis(self):
        # A persistent drift with almost no bar-to-bar noise: the displacement
        # is enormous relative to this instrument's own volatility.
        closes = [100.0 * (1.001 ** i) + (1e-7 if i % 2 else 0.0) for i in range(200)]
        result = normalized_return(closes, window=12)
        assert result is not None
        assert result.z == pytest.approx(6.0)

    def test_a_single_spike_does_not_reach_the_clamp(self):
        # Inverse of the above, and the reason the clamp is rarely hit: a lone
        # spike inflates the volatility it is then divided by.
        closes = [100.0 + (i % 3) * 0.01 for i in range(200)]
        closes[-1] = 10_000.0
        result = normalized_return(closes, window=12)
        assert result is not None
        assert 0 < result.z < 6.0


class TestPivots:
    def test_pivot_requires_confirmation_on_both_sides(self):
        # A peak in the final bars cannot be a pivot: the right-hand bars that
        # would confirm it do not exist yet.
        rows = [(10, 10.5, 9.5, 10)] * 6 + [(10, 20.0, 9.5, 10)]
        pivots = swing_pivots(series(rows), strength=2)
        assert all(p.index != len(rows) - 1 for p in pivots)

    def test_finds_an_interior_high_and_low(self):
        rows = [
            (10, 10.5, 9.5, 10), (10, 10.5, 9.5, 10),
            (10, 15.0, 9.5, 10),            # index 2: swing high
            (10, 10.5, 9.5, 10), (10, 10.5, 9.5, 10),
            (10, 10.5, 5.0, 10),            # index 5: swing low
            (10, 10.5, 9.5, 10), (10, 10.5, 9.5, 10),
        ]
        pivots = swing_pivots(series(rows), strength=2)
        assert any(p.index == 2 and p.kind == "HIGH" for p in pivots)
        assert any(p.index == 5 and p.kind == "LOW" for p in pivots)


class TestRegime:
    def test_reports_insufficient_data_rather_than_guessing(self):
        assert volatility_regime(series([(10, 11, 9, 10)] * 5)).label == "INSUFFICIENT_DATA"

    def test_labels_are_descriptive_and_carry_no_direction(self):
        regime = volatility_regime(series([(10, 11, 9, 10)] * 150))
        assert regime.label in {
            "COMPRESSED", "NORMAL", "ELEVATED", "EXPANDED", "UNRANKED", "INSUFFICIENT_DATA",
        }
        assert regime.atr is not None


# --------------------------------------------------------------------------
# order blocks
# --------------------------------------------------------------------------
def bullish_ob_candles() -> list[Candle]:
    """A sequence that satisfies all three bullish rules exactly once."""
    rows: list[tuple[float, float, float, float]] = []
    # 20 quiet bars establishing an ATR of roughly 1.0.
    for _ in range(20):
        rows.append((100.0, 100.5, 99.5, 100.0))
    # A swing high at index 20 that later becomes the break-of-structure level.
    rows.append((100.0, 103.0, 99.5, 100.0))
    for _ in range(4):
        rows.append((100.0, 100.5, 99.5, 100.0))
    # Origin: a down-close candle (index 25). Its high matches the quiet bars
    # so it cannot itself create a trivial local swing high -- that would make
    # the most recent pivot a meaningless 100.5 instead of the intended 103.0.
    rows.append((100.0, 100.5, 99.0, 99.2))
    # Displacement (index 26): a large up-close candle closing above the 103.0
    # swing high, i.e. a genuine break of structure.
    rows.append((99.2, 106.0, 99.0, 105.5))
    # Quiet bars afterwards that never revisit the zone.
    for _ in range(6):
        rows.append((105.5, 106.0, 105.0, 105.5))
    return series(rows)


# Index of the displacement candle in `bullish_ob_candles`, named so the
# no-look-ahead test cannot drift out of sync with the fixture.
DISPLACEMENT_INDEX = 26


class TestOrderBlockRules:
    def test_detects_a_zone_that_satisfies_every_published_rule(self):
        zones = orderblocks.detect(bullish_ob_candles(), symbol="TEST", timeframe="H1")
        assert len(zones) == 1
        zone = zones[0]
        assert zone.side == "BULLISH"
        # The zone is the origin candle's full range.
        assert zone.zone_low == pytest.approx(99.0)
        assert zone.zone_high == pytest.approx(100.5)
        # Displacement cleared the configured ATR multiple.
        assert zone.displacement_atr >= 1.0
        # The structure break is the prior swing high, and the displacement
        # candle closed above it.
        assert zone.bos_level == pytest.approx(103.0)
        assert zone.confirmed_ts > zone.origin_ts

    def test_no_zone_without_a_break_of_structure(self):
        rows = [(100.0, 100.5, 99.5, 100.0)] * 20
        rows.append((100.0, 130.0, 99.5, 100.0))   # a very high swing high
        rows += [(100.0, 100.5, 99.5, 100.0)] * 4
        rows.append((100.0, 100.5, 99.0, 99.2))    # origin
        rows.append((99.2, 106.0, 99.0, 105.5))    # displacement below the swing
        rows += [(105.5, 106.0, 105.0, 105.5)] * 6
        zones = orderblocks.detect(series(rows), symbol="TEST", timeframe="H1")
        assert zones == []

    def test_no_zone_without_sufficient_displacement(self):
        rows = [(100.0, 100.5, 99.5, 100.0)] * 20
        rows.append((100.0, 103.0, 99.5, 100.0))
        rows += [(100.0, 100.5, 99.5, 100.0)] * 4
        rows.append((100.0, 100.5, 99.0, 99.2))
        # Closes above the swing high, but the body is far below 1.0 x ATR.
        rows.append((103.1, 103.3, 103.0, 103.2))
        rows += [(103.2, 103.4, 103.0, 103.2)] * 6
        zones = orderblocks.detect(series(rows), symbol="TEST", timeframe="H1")
        assert zones == []

    def test_returns_nothing_rather_than_guessing_on_short_history(self):
        assert orderblocks.detect(series([(10, 11, 9, 10)] * 8), symbol="T", timeframe="H1") == []


class TestZoneLifecycle:
    def _with_tail(self, tail: list[tuple[float, float, float, float]]):
        base = bullish_ob_candles()[: DISPLACEMENT_INDEX + 1]   # through the displacement
        start = base[-1].ts + timedelta(hours=1)
        return base + series(tail, start=start)

    def test_untouched_zone_stays_fresh(self):
        zones = orderblocks.detect(
            self._with_tail([(105.5, 106.0, 105.0, 105.5)] * 6), symbol="T", timeframe="H1"
        )
        assert zones[0].status == orderblocks.FRESH
        assert zones[0].events == []

    def test_touching_the_zone_marks_it_tapped(self):
        zones = orderblocks.detect(
            self._with_tail([(105.0, 105.5, 100.3, 105.0)] + [(105.0, 105.5, 104.5, 105.0)] * 5),
            symbol="T", timeframe="H1",
        )
        assert zones[0].status == orderblocks.TAPPED
        assert [e.kind for e in zones[0].events] == [orderblocks.TAPPED]

    def test_trading_through_the_midpoint_marks_it_mitigated(self):
        zones = orderblocks.detect(
            self._with_tail([(105.0, 105.5, 99.5, 105.0)] + [(105.0, 105.5, 104.5, 105.0)] * 5),
            symbol="T", timeframe="H1",
        )
        assert zones[0].status == orderblocks.MITIGATED
        kinds = [e.kind for e in zones[0].events]
        assert orderblocks.TAPPED in kinds and orderblocks.MITIGATED in kinds

    def test_closing_below_the_zone_invalidates_it_and_stops_the_walk(self):
        zones = orderblocks.detect(
            self._with_tail([(105.0, 105.5, 98.0, 98.5)] + [(98.5, 99.0, 98.0, 98.5)] * 5),
            symbol="T", timeframe="H1",
        )
        zone = zones[0]
        assert zone.status == orderblocks.INVALIDATED
        assert zone.events[-1].kind == orderblocks.INVALIDATED
        # Terminal: nothing is recorded after invalidation.
        assert sum(1 for e in zone.events if e.kind == orderblocks.INVALIDATED) == 1

    def test_invalidated_zones_are_excluded_from_nearest(self):
        zones = orderblocks.detect(
            self._with_tail([(105.0, 105.5, 98.0, 98.5)] + [(98.5, 99.0, 98.0, 98.5)] * 5),
            symbol="T", timeframe="H1",
        )
        dead = [z for z in zones if z.status == orderblocks.INVALIDATED]
        assert dead, "expected the bullish zone to have been invalidated"
        nearest = orderblocks.nearest_zone(zones)
        # The sell-off is itself a bearish displacement, so another zone may
        # legitimately exist; what must never happen is the dead one coming
        # back as a level to watch.
        assert nearest is None or nearest.zone_id not in {z.zone_id for z in dead}


class TestDeterminismAndIdentity:
    def test_detection_is_deterministic(self):
        candles = bullish_ob_candles()
        first = orderblocks.detect(candles, symbol="T", timeframe="H1")
        second = orderblocks.detect(candles, symbol="T", timeframe="H1")
        assert [z.as_dict() for z in first] == [z.as_dict() for z in second]

    def test_zone_identity_is_stable_across_runs(self):
        candles = bullish_ob_candles()
        a = orderblocks.detect(candles, symbol="T", timeframe="H1")[0]
        b = orderblocks.detect(candles, symbol="T", timeframe="H1")[0]
        assert a.zone_id == b.zone_id

    def test_identity_separates_symbol_timeframe_and_side(self):
        candles = bullish_ob_candles()
        one = orderblocks.detect(candles, symbol="AAA", timeframe="H1")[0]
        two = orderblocks.detect(candles, symbol="BBB", timeframe="H1")[0]
        three = orderblocks.detect(candles, symbol="AAA", timeframe="M15")[0]
        assert len({one.zone_id, two.zone_id, three.zone_id}) == 3

    def test_event_ids_are_unique_and_reproducible(self):
        candles = bullish_ob_candles()[:28]
        start = candles[-1].ts + timedelta(hours=1)
        tail = series([(105.0, 105.5, 99.5, 105.0)] + [(105.0, 105.5, 104.5, 105.0)] * 5, start=start)
        zones = orderblocks.detect(candles + tail, symbol="T", timeframe="H1")
        again = orderblocks.detect(candles + tail, symbol="T", timeframe="H1")
        ids = [e.event_id for e in zones[0].events]
        assert len(ids) == len(set(ids))
        assert ids == [e.event_id for e in again[0].events]

    def test_no_look_ahead_a_zone_cannot_exist_before_its_displacement_closed(self):
        """Truncating history to just before the displacement candle must
        remove the zone entirely -- the single strongest no-look-ahead check."""
        full = bullish_ob_candles()
        zones_full = orderblocks.detect(full, symbol="T", timeframe="H1")
        assert len(zones_full) == 1

        # Cut off the displacement candle that confirmed the zone.
        truncated = full[:DISPLACEMENT_INDEX]
        assert orderblocks.detect(truncated, symbol="T", timeframe="H1") == []

    def test_scores_are_declared_descriptive_not_probabilistic(self):
        zone = orderblocks.detect(bullish_ob_candles(), symbol="T", timeframe="H1")[0]
        assert 0.0 <= zone.score <= 100.0
        basis = zone.score_basis.lower()
        assert "not a win rate" in basis and "not a probability" in basis


# --------------------------------------------------------------------------
# market clock
# --------------------------------------------------------------------------
class TestMarketClock:
    def test_saturday_is_closed(self):
        assert not clock.is_market_open(datetime(2026, 10, 31, 12, 0, tzinfo=UTC))

    def test_midweek_is_open(self):
        assert clock.is_market_open(datetime(2026, 10, 28, 12, 0, tzinfo=UTC))

    def test_friday_closes_in_the_evening(self):
        assert clock.is_market_open(datetime(2026, 10, 30, 20, 0, tzinfo=UTC))
        assert not clock.is_market_open(datetime(2026, 10, 30, 22, 0, tzinfo=UTC))

    def test_sunday_opens_in_the_evening(self):
        assert not clock.is_market_open(datetime(2026, 11, 1, 12, 0, tzinfo=UTC))
        assert clock.is_market_open(datetime(2026, 11, 1, 22, 0, tzinfo=UTC))

    def test_replay_is_never_labelled_live(self):
        """The core honesty guarantee: recorded history cannot be LIVE."""
        status = clock.replay_status(
            now=datetime(2026, 10, 31, 3, 0, tzinfo=UTC),
            cursor=datetime(2026, 9, 11, 20, 55, tzinfo=UTC),
            store_last_bar=datetime(2026, 9, 11, 20, 55, tzinfo=UTC),
        )
        assert status.status is clock.FeedStatus.REPLAY
        assert status.replay is True
        assert "not a live feed" in status.detail.lower()

    def test_a_stale_feed_is_reported_stale_not_live(self):
        now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
        status = clock.live_status(now=now, feed_last_tick=now - timedelta(minutes=30))
        assert status.status is clock.FeedStatus.STALE

    def test_a_fresh_feed_in_market_hours_is_live(self):
        now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
        status = clock.live_status(now=now, feed_last_tick=now - timedelta(seconds=5))
        assert status.status is clock.FeedStatus.LIVE

    def test_a_fresh_feed_outside_market_hours_is_closed_not_live(self):
        now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)   # Saturday
        status = clock.live_status(now=now, feed_last_tick=now - timedelta(seconds=5))
        assert status.status is clock.FeedStatus.CLOSED

    def test_no_sessions_are_active_while_the_market_is_shut(self):
        assert clock.active_sessions(datetime(2026, 10, 31, 12, 0, tzinfo=UTC)) == ()


# --------------------------------------------------------------------------
# universe
# --------------------------------------------------------------------------
class TestUniverse:
    def test_every_instrument_declares_its_legs_and_precision(self):
        for symbol in universe.SYMBOLS:
            meta = universe.get(symbol)
            assert meta.base and meta.quote
            assert meta.price_precision > 0
            assert meta.pip_size > 0

    def test_unknown_symbols_raise_rather_than_resolving_to_something_else(self):
        with pytest.raises(KeyError):
            universe.get("NOTAPAIR")

    def test_jpy_crosses_use_a_two_decimal_pip(self):
        assert universe.get("USDJPY").pip_size == pytest.approx(0.01)
        assert universe.get("EURUSD").pip_size == pytest.approx(0.0001)

    def test_currency_coverage_is_reported_truthfully(self):
        # These are facts about the data on this machine, and the strength
        # model depends on them being reported rather than assumed even.
        assert len(universe.pairs_for_currency("NZD")) == 1
        assert len(universe.pairs_for_currency("CHF")) == 2
        assert len(universe.pairs_for_currency("USD")) == 6

    def test_xauusd_is_a_metal_and_not_part_of_currency_strength(self):
        assert universe.get("XAUUSD").asset_class == "METAL"
        assert "XAUUSD" not in universe.FX_PAIRS
