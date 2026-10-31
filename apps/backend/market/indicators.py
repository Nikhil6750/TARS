"""Deterministic indicator maths: Wilder ATR, volatility-normalised returns,
swing pivots and volatility regime.

Pure functions over completed candles. No randomness, no smoothing that
invents observations, no probability claims. Given the same candles these
return the same numbers, which is what makes the dashboard's output checkable
by hand.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from market.store import Candle

ATR_PERIOD = 14
# Window used to judge whether current ATR is high or low *for this
# instrument*, so a regime label never compares gold's range to EURUSD's.
REGIME_LOOKBACK = 100


def true_range(prev_close: float, candle: Candle) -> float:
    return max(
        candle.high - candle.low,
        abs(candle.high - prev_close),
        abs(candle.low - prev_close),
    )


def wilder_atr(candles: list[Candle], period: int = ATR_PERIOD) -> list[float | None]:
    """Wilder's ATR, aligned index-for-index with `candles`.

    The first `period` entries are None: an ATR does not exist until a full
    period of true ranges has been seen. The seed is the simple mean of the
    first `period` true ranges and every later value is Wilder's recursive
    smoothing, i.e. ``(prev * (period - 1) + tr) / period``.
    """
    out: list[float | None] = [None] * len(candles)
    if len(candles) < period + 1:
        return out

    trs = [true_range(candles[i - 1].close, candles[i]) for i in range(1, len(candles))]
    atr = sum(trs[:period]) / period
    out[period] = atr
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
        out[i + 1] = atr
    return out


def log_returns(closes: list[float]) -> list[float]:
    """Log returns, length ``len(closes) - 1``. Non-positive prices are
    skipped as zero rather than raising, so one bad tick cannot blank a
    whole series."""
    out: list[float] = []
    for a, b in zip(closes, closes[1:], strict=False):
        out.append(math.log(b / a) if a > 0 and b > 0 else 0.0)
    return out


def stdev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


@dataclass(frozen=True)
class NormalizedReturn:
    """A return expressed in units of the instrument's own recent volatility.

    `z` is comparable across instruments, which is the only reason gold and
    EURUSD can share one axis on the X-Ray. It is a measure of price
    displacement, not of money flow and not of a probability.
    """
    raw_return: float      # log return over the window
    pct_change: float      # the same move as a percentage, for display
    volatility: float      # stdev of per-bar log returns over the window
    z: float               # raw_return / volatility, clamped
    bars_used: int

    def as_dict(self) -> dict:
        return {
            "raw_return": self.raw_return,
            "pct_change": self.pct_change,
            "volatility": self.volatility,
            "z": self.z,
            "bars_used": self.bars_used,
        }


# Beyond this the value says only "extreme"; clamping stops one outlier from
# flattening every other instrument on a shared axis.
Z_CLAMP = 6.0


def normalized_return(closes: list[float], *, window: int, vol_window: int = 100) -> NormalizedReturn | None:
    """Displacement over the last `window` bars, scaled by the per-bar
    volatility measured over `vol_window` bars."""
    if len(closes) < window + 2:
        return None
    start, end = closes[-(window + 1)], closes[-1]
    if start <= 0 or end <= 0:
        return None

    raw = math.log(end / start)
    pct = (end / start - 1.0) * 100.0
    per_bar = log_returns(closes[-(vol_window + 1):])
    vol = stdev(per_bar)
    # Scale the single-bar volatility up to the horizon actually measured, so
    # a 24-bar move is compared against 24 bars' worth of typical movement
    # (a random-walk scaling, stated as such, not a forecast).
    horizon_vol = vol * math.sqrt(window)
    if horizon_vol <= 0:
        return None

    z = max(-Z_CLAMP, min(Z_CLAMP, raw / horizon_vol))
    return NormalizedReturn(
        raw_return=raw,
        pct_change=pct,
        volatility=horizon_vol,
        z=z,
        bars_used=min(len(closes) - 1, max(window, vol_window)),
    )


@dataclass(frozen=True)
class Regime:
    atr: float | None
    atr_pct_of_price: float | None
    percentile: float | None   # where current ATR sits in its own recent range
    label: str                 # descriptive only

    def as_dict(self) -> dict:
        return {
            "atr": self.atr,
            "atr_pct_of_price": self.atr_pct_of_price,
            "percentile": self.percentile,
            "label": self.label,
        }


def volatility_regime(candles: list[Candle], *, period: int = ATR_PERIOD,
                      lookback: int = REGIME_LOOKBACK) -> Regime:
    """Current ATR plus where it sits among its own recent values.

    The label is a description of realised range only. It carries no
    directional view and no expectation about what happens next.
    """
    series = wilder_atr(candles, period)
    current = series[-1] if series else None
    if current is None or not candles:
        return Regime(None, None, None, "INSUFFICIENT_DATA")

    price = candles[-1].close
    atr_pct = (current / price * 100.0) if price > 0 else None

    history = [v for v in series[-lookback:] if v is not None]
    percentile = None
    if len(history) >= 20:
        below = sum(1 for v in history if v <= current)
        percentile = below / len(history) * 100.0

    if percentile is None:
        label = "UNRANKED"
    elif percentile >= 80:
        label = "EXPANDED"
    elif percentile >= 55:
        label = "ELEVATED"
    elif percentile >= 25:
        label = "NORMAL"
    else:
        label = "COMPRESSED"
    return Regime(current, atr_pct, percentile, label)


@dataclass(frozen=True)
class Pivot:
    index: int
    ts: str
    price: float
    kind: str   # HIGH | LOW


def swing_pivots(candles: list[Candle], *, strength: int = 2) -> list[Pivot]:
    """Confirmed fractal swing highs and lows.

    A pivot at index `i` requires `strength` bars either side, so it is only
    recognised once those later bars have completed. That delay is deliberate:
    it is what makes a pivot a fact about closed bars rather than a guess
    about the bar still forming.
    """
    pivots: list[Pivot] = []
    for i in range(strength, len(candles) - strength):
        window = candles[i - strength: i + strength + 1]
        candle = candles[i]
        if all(candle.high >= c.high for c in window) and any(
            candle.high > c.high for c in window
        ):
            pivots.append(Pivot(i, candle.ts.isoformat(), candle.high, "HIGH"))
        if all(candle.low <= c.low for c in window) and any(
            candle.low < c.low for c in window
        ):
            pivots.append(Pivot(i, candle.ts.isoformat(), candle.low, "LOW"))
    return pivots
