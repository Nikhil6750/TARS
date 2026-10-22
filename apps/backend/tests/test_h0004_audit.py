"""Technical audit of the FROZEN H0004 rule (Asian Range Breakout at London Open, XAUUSD H1) BEFORE it is executed.
Everything here uses synthetic, hand-computable examples plus calendar facts - no performance data of any kind."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.backtest import simulate
from research.costs import CostModel
from research.features import compute_features, evaluate_conditions, verify_causality
from research.schema import make_bars
from research.sessions import classify_sessions
from research.spec import parse_spec

FROZEN_HASH = "49635134306f040a466c955b8c70fb9112414680033494cdfb1a2f124fcb3bdd"
SPEC_PATH = Path(__file__).parent / "fixtures_research" / "h0004_spec.json"


@pytest.fixture(scope="module")
def spec():
    text = SPEC_PATH.read_text(encoding="utf-8")
    s = parse_spec(text)
    assert s.spec_hash() == FROZEN_HASH, "the frozen H0004 spec must never change"
    return s


def utc(s):
    return pd.Timestamp(s, tz="UTC")


def h1_bars(n=40, start="2024-03-04 00:00", base=2000.0, spread=0.5, highs=None, closes=None, lows=None, opens=None):
    ts = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    c = np.full(n, base) if closes is None else np.asarray(closes, float)
    o = np.concatenate([[base], c[:-1]]) if opens is None else np.asarray(opens, float)
    hi = np.maximum(o, c) + 0.2 if highs is None else np.asarray(highs, float)
    lo = np.minimum(o, c) - 0.2 if lows is None else np.asarray(lows, float)
    df = pd.DataFrame({"timestamp_utc": ts, "bid_open": o, "bid_high": hi, "bid_low": lo, "bid_close": c})
    for f in ("open", "high", "low", "close"):
        df[f"ask_{f}"] = df[f"bid_{f}"] + spread
    df["spread"] = spread
    df["volume"] = 1.0
    return make_bars(df, instrument="XAUUSD", provider="t", source_timeframe="H1")


# ============================ frozen spec integrity =========================================================================
def test_frozen_spec_is_exactly_what_was_registered(spec):
    assert spec.instruments == ["XAUUSD"] and spec.data.timeframe == "H1" and spec.signal.entry_sessions == ["LONDON"]
    assert [f.kind for f in spec.features] == ["rolling_high", "rolling_low", "atr"] and [f.n for f in spec.features] == [9, 9, 14]
    assert spec.exit.stop_atr_mult == 1.5 and spec.exit.target_r == 2.0 and spec.exit.max_hold_bars == 24
    assert spec.costs.commission_round_turn == 0.07 and spec.costs.slippage_per_side == 0.02 and spec.costs.latency_bars == 0
    assert not spec.diagnostic and spec.rejection.min_trades_per_period >= 100


# ============================ A. rolling high/low look-ahead ================================================================
def test_rolling_extremes_exclude_the_current_candle(spec):
    highs = [2000.0] * 9 + [2005.0, 2001.0]            # bar 9 spikes; bar 10 normal
    closes = [1999.0] * 9 + [2003.0, 2000.0]
    b = h1_bars(11, highs=highs, closes=closes, opens=[1999.0] * 11, lows=[1998.0] * 11, spread=0.0)   # zero spread: features use MID prices
    f = compute_features(b, spec.features)
    assert f["asian_high"].iloc[9] == 2000.0           # max of bars 0..8, NOT including bar 9's own 2005 high
    assert f["asian_high"].iloc[10] == 2005.0          # the spike enters the window only from the NEXT bar
    assert np.isnan(f["asian_high"].iloc[8])           # not enough prior history -> no value, never a partial window


def test_a_close_can_legitimately_exceed_the_prior_window_and_crosses_exactly_once(spec):
    closes = [1999.0] * 12 + [2003.0, 2004.0, 2004.5]
    b = h1_bars(15, highs=[2000.0] * 12 + [2003.4, 2004.4, 2004.9], closes=closes, opens=[1999.0] * 15, lows=[1998.0] * 15, spread=0.0)
    f = compute_features(b, spec.features)
    cross = evaluate_conditions(f, spec.signal.long_when)
    assert list(np.flatnonzero(cross.to_numpy())) == [12]     # fires on the first close above the prior 9-bar high, not on continuation bars
    # an intrabar spike that closes back inside the range does not trigger
    b2 = h1_bars(12, highs=[2000.0] * 9 + [2010.0, 2000.0, 2000.0], closes=[1999.0] * 12, opens=[1999.0] * 12, lows=[1998.0] * 12, spread=0.0)
    assert not evaluate_conditions(compute_features(b2, spec.features), spec.signal.long_when).any()


def test_features_are_causal_on_real_shaped_data(spec):
    rng = np.random.default_rng(1)
    c = 2000 + np.cumsum(rng.normal(0, 1.0, 400))
    b = h1_bars(400, closes=c, highs=c + rng.random(400), lows=c - rng.random(400))
    assert verify_causality(b, spec.features) == []


# ============================ B. 'Asian range' fidelity ==================================================================
def _window_bars(ts_start: str, k: int) -> pd.DatetimeIndex:
    """UTC start times of the 9 H1 bars in the asian_high/low window of the signal bar starting at ts_start (window = previous 9 bars)."""
    t = utc(ts_start)
    return pd.date_range(t - pd.Timedelta(hours=9), t - pd.Timedelta(hours=1), freq="1h")


@pytest.mark.parametrize("season,first_london_utc", [("winter", "2024-01-16 08:00"), ("summer", "2024-07-16 07:00")])
def test_only_the_first_london_bar_has_a_pure_pre_london_window(season, first_london_utc):
    """The window is 'previous 9 hourly bars', not 'the Asian session'. At the FIRST London bar it is Asian/pre-London; every later
    London-only entry bar slides London hours into the window. Documented fidelity limit of the frozen rule (NOT a look-ahead)."""
    first = utc(first_london_utc)
    contaminated = []
    for k in range(5):                                       # London-only H1 entry bars: 08:00..12:00 London local = 5 bars
        bar = first + pd.Timedelta(hours=k)
        assert classify_sessions([bar]).iloc[0] == "LONDON"
        win = classify_sessions(_window_bars(str(bar)[:16], 9))
        contaminated.append(bool((win == "LONDON").any()))
    assert contaminated == [False, True, True, True, True]
    # and the bar just before London open is not an eligible entry session at all
    assert classify_sessions([first - pd.Timedelta(hours=1)]).iloc[0] != "LONDON"


def test_window_covers_tokyo_session_at_the_first_london_bar():
    win = classify_sessions(_window_bars("2024-01-16 08:00", 9))
    assert set(win) <= {"ASIA", "OFF"}                        # 23:00..07:00 UTC: Tokyo afternoon + quiet hours, no London/NY
    assert "ASIA" in set(win)


# ============================ C. signal timing / executable entry ===========================================================
def _breakout_market(spread=0.5):
    """Flat 2000 for 30 bars, a London-session bar closes at 2003 (> prior 9-bar high 2000.2), then drifts to hit the 2R target."""
    n = 100
    ts = pd.date_range("2024-01-15 00:00", periods=n, freq="1h", tz="UTC")    # long enough warm-up for ATR(14) before the signal
    c = np.full(n, 2000.0)
    sig = int(np.flatnonzero(ts == utc("2024-01-16 09:00"))[0])
    c[sig:] = 2003.0
    c[sig + 1:] = np.linspace(2003.0, 2040.0, n - sig - 1)
    o = np.concatenate([[2000.0], c[:-1]])
    hi = np.maximum(o, c) + 0.1
    lo = np.minimum(o, c) - 0.1
    df = pd.DataFrame({"timestamp_utc": ts, "bid_open": o, "bid_high": hi, "bid_low": lo, "bid_close": c})
    for f in ("open", "high", "low", "close"):
        df[f"ask_{f}"] = df[f"bid_{f}"] + spread
    df["spread"] = spread
    df["volume"] = 1.0
    return make_bars(df, instrument="XAUUSD", provider="t", source_timeframe="H1"), sig


def _sim(spec, bars, costs=None):
    spec = spec.model_copy(update={"costs": costs or spec.costs})
    return simulate(bars, spec, spec.costs, start=bars["timestamp_utc"].iloc[0], end=bars["timestamp_utc"].iloc[-1] + pd.Timedelta("1h"))


def test_signal_forms_on_a_completed_bar_and_fills_at_the_next_bars_open_ask(spec):
    bars, sig = _breakout_market()
    t = _sim(spec, bars)
    assert len(t) >= 1
    first = t.iloc[0]
    assert first["signal_time"] == bars["timestamp_utc"].iloc[sig] and first["entry_time"] == bars["timestamp_utc"].iloc[sig + 1]
    nxt = bars.iloc[sig + 1]
    assert first["entry_price"] == pytest.approx(nxt["ask_open"] + spec.costs.slippage_per_side)       # executable ask + slippage
    assert first["entry_price"] != pytest.approx(bars["bid_close"].iloc[sig])                          # NOT the already-known signal close
    assert first["direction"] == 1 and first["session"] == "LONDON"


def test_signals_outside_the_london_session_never_trade(spec):
    bars, _ = _breakout_market()
    t = _sim(spec, bars)
    assert (classify_sessions(t["signal_time"]) == "LONDON").all()


# ============================ D. costs and intrabar handling ================================================================
def test_net_vs_gross_difference_is_exactly_spread_slippage_commission(spec):
    bars, sig = _breakout_market(spread=0.5)
    t = _sim(spec, bars).iloc[0]
    e, x = bars.iloc[sig + 1], bars[bars["timestamp_utc"] == t["exit_time"]].iloc[0]
    exit_spread = x["ask_close"] - x["bid_close"]
    expected_gap = 0.5 / 2 + exit_spread / 2 + 2 * spec.costs.slippage_per_side + spec.costs.commission_round_turn
    assert t["gross_pnl"] - t["net_pnl"] == pytest.approx(expected_gap, abs=1e-9)         # spread charged ONCE per side, no double count
    assert t["cost_r"] == pytest.approx(expected_gap / t["risk"])


def test_commission_is_in_price_units_per_round_turn_not_per_side(spec):
    bars, _ = _breakout_market()
    base = _sim(spec, bars).iloc[0]
    doubled = _sim(spec, bars, spec.costs.model_copy(update={"commission_round_turn": spec.costs.commission_round_turn * 2})).iloc[0]
    assert base["net_pnl"] - doubled["net_pnl"] == pytest.approx(spec.costs.commission_round_turn)


def test_stop_and_target_in_the_same_bar_resolves_to_the_stop(spec):
    bars, sig = _breakout_market()
    bars = bars.copy()
    j = sig + 1                                                # entry bar: make its range hit both a 1.5ATR stop and a 2R target
    for side in ("bid", "ask"):
        bars.loc[j, f"{side}_high"] += 500.0
        bars.loc[j, f"{side}_low"] -= 500.0
    t = _sim(spec, bars).iloc[0]
    assert t["exit_reason"] == "STOP" and t["net_r"] < 0


def test_gap_through_the_stop_fills_at_the_gapped_open_not_at_the_stop_level(spec):
    bars, sig = _breakout_market()
    bars = bars.copy()
    k = sig + 3
    for side, off in (("bid", 0.0), ("ask", 0.5)):
        for f in ("open", "high", "low", "close"):
            bars.loc[k, f"{side}_{f}"] = 1900.0 + off        # violent gap far below any stop
    t = _sim(spec, bars).iloc[0]
    assert t["exit_reason"] == "STOP" and t["exit_price"] < t["entry_price"] - 50 and t["net_r"] < -5      # worse than -1R: gap risk is real


def test_stress_spread_increases_cost_exactly_by_half_extra_spread_per_side(spec):
    bars, sig = _breakout_market(spread=0.5)
    c1 = _sim(spec, bars, spec.costs).iloc[0]
    c2 = _sim(spec, bars, spec.costs.with_stress(spread_mult=2.0)).iloc[0]
    assert c1["net_pnl"] - c2["net_pnl"] == pytest.approx(0.5 / 2 + 0.5 / 2, abs=1e-9)    # +0.25 entry, +0.25 exit


def test_financing_is_a_disabled_placeholder_and_reported_as_such(spec):
    assert spec.costs.financing_per_bar == 0.0 and spec.costs.financing_is_placeholder
    bars, _ = _breakout_market()
    # a 24-hour maximum hold means trades can span the daily break/weekend: overnight swap is NOT modelled (disclosed limitation)
    assert spec.exit.max_hold_bars == 24


# ============================ E. data integrity ===============================================================================
def test_validation_ends_exactly_where_final_untouched_begins(spec):
    assert spec.development.end == spec.validation.start and spec.validation.end == spec.untouched_test.start
    assert spec.untouched_test.start == pd.Timestamp("2025-10-01", tz="UTC").to_pydatetime()
