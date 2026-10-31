"""The Market X-Ray series.

Each instrument contributes one line: its volatility-normalised return (`z`)
measured at every completed bar in the view window. Lines above the zero axis
are instruments that have risen over the trailing window, lines below have
fallen, and the vertical distance is how large that move is relative to the
instrument's own typical movement -- which is the only reason gold and EURUSD
can be read on one axis.

What the X-Ray is, and is not:

* It **is** price-based directional displacement computed from recorded
  bid/ask candles.
* It is **not** institutional money flow, order flow, volume delivery, or a
  forecast. Nothing in this module observes who traded or why.

Every point returned corresponds to one real completed bar and carries that
bar's timestamp, close and source timeframe, so the UI can show provenance on
hover instead of asserting it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from market.indicators import Z_CLAMP, log_returns, stdev

# Trailing window (in bars of the chosen timeframe) over which displacement is
# measured, and how many points the view returns.
DEFAULT_RETURN_WINDOW = 12
DEFAULT_POINTS = 90
VOL_WINDOW = 100


@dataclass
class XRayPoint:
    ts: str
    z: float
    close: float
    pct_change: float

    def as_dict(self) -> dict:
        return {
            "ts": self.ts,
            "z": round(self.z, 4),
            "close": self.close,
            "pct_change": round(self.pct_change, 4),
        }


@dataclass
class XRaySeries:
    symbol: str
    asset_class: str
    timeframe: str
    direction: str            # BULLISH | BEARISH | FLAT
    current_z: float
    current_close: float
    pct_change: float
    rank: int
    points: list[XRayPoint] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "asset_class": self.asset_class,
            "timeframe": self.timeframe,
            "direction": self.direction,
            "current_z": round(self.current_z, 4),
            "current_close": self.current_close,
            "pct_change": round(self.pct_change, 4),
            "rank": self.rank,
            "points": [p.as_dict() for p in self.points],
        }


@dataclass
class XRaySnapshot:
    timeframe: str
    return_window: int
    as_of: str
    series: list[XRaySeries]
    skipped: dict[str, str]
    measures: str

    def as_dict(self) -> dict:
        return {
            "timeframe": self.timeframe,
            "return_window": self.return_window,
            "as_of": self.as_of,
            "series": [s.as_dict() for s in self.series],
            "skipped": self.skipped,
            "measures": self.measures,
        }


_MEASURES = (
    "Volatility-normalised price return over a trailing window, per completed "
    "bar. Price-based directional movement only -- not institutional money "
    "flow and not a forecast."
)


def _rolling_z(
    closes: list[float],
    *,
    window: int,
    points: int,
    vol_window: int = VOL_WINDOW,
) -> list[tuple[int, float, float]]:
    """(index, z, pct_change) at each of the last `points` bars.

    `index` indexes into `closes`. Returns fewer points than asked for when
    history is short rather than padding with invented values.
    """
    need = window + vol_window + 1
    first = max(need, len(closes) - points)
    out: list[tuple[int, float, float]] = []

    for i in range(first, len(closes)):
        start, end = closes[i - window], closes[i]
        if start <= 0 or end <= 0:
            continue
        raw = math.log(end / start)
        per_bar = log_returns(closes[i - vol_window: i + 1])
        horizon_vol = stdev(per_bar) * math.sqrt(window)
        if horizon_vol <= 0:
            continue
        z = max(-Z_CLAMP, min(Z_CLAMP, raw / horizon_vol))
        out.append((i, z, (end / start - 1.0) * 100.0))
    return out


def build_xray(
    store,
    *,
    as_of,
    timeframe: str,
    return_window: int = DEFAULT_RETURN_WINDOW,
    points: int = DEFAULT_POINTS,
    symbols: list[str] | None = None,
) -> XRaySnapshot:
    """Build the X-Ray for every instrument with enough history at `as_of`."""
    from market import universe

    targets = symbols or list(store.available_symbols)
    series: list[XRaySeries] = []
    skipped: dict[str, str] = {}
    limit = return_window + VOL_WINDOW + points + 2

    for symbol in targets:
        try:
            rows = store.close_series(symbol, timeframe, as_of=as_of, limit=limit)
        except FileNotFoundError:
            skipped[symbol] = "no candles for " + timeframe
            continue
        closes = [c for _, c in rows]
        if len(closes) < return_window + VOL_WINDOW + 2:
            skipped[symbol] = (
                "needs " + str(return_window + VOL_WINDOW + 2) + " bars, has " + str(len(closes))
            )
            continue

        zs = _rolling_z(closes, window=return_window, points=points)
        if not zs:
            skipped[symbol] = "no computable points"
            continue

        meta = universe.INSTRUMENTS.get(symbol)
        current_z, current_pct = zs[-1][1], zs[-1][2]
        series.append(
            XRaySeries(
                symbol=symbol,
                asset_class=meta.asset_class if meta else "UNKNOWN",
                timeframe=timeframe,
                direction="BULLISH" if current_z > 0.1 else "BEARISH" if current_z < -0.1 else "FLAT",
                current_z=current_z,
                current_close=closes[-1],
                pct_change=current_pct,
                rank=0,
                points=[
                    XRayPoint(ts=rows[i][0].isoformat(), z=z, close=closes[i], pct_change=pct)
                    for i, z, pct in zs
                ],
            )
        )

    # Rank 1 = strongest. Ranking is purely the current normalised return.
    series.sort(key=lambda s: -s.current_z)
    for position, s in enumerate(series, start=1):
        s.rank = position

    return XRaySnapshot(
        timeframe=timeframe,
        return_window=return_window,
        as_of=as_of.isoformat(),
        series=series,
        skipped=skipped,
        measures=_MEASURES,
    )


@dataclass
class LeaderCard:
    symbol: str
    asset_class: str
    close: float
    pct_change: float
    z: float
    rank: int
    direction: str
    atr: float | None
    regime: str
    spread: float | None
    nearest_zone: dict | None

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "asset_class": self.asset_class,
            "close": self.close,
            "pct_change": round(self.pct_change, 4),
            "z": round(self.z, 4),
            "rank": self.rank,
            "direction": self.direction,
            "atr": self.atr,
            "regime": self.regime,
            "spread": self.spread,
            "nearest_zone": self.nearest_zone,
        }
