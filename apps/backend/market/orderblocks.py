"""Objective order-block detection and zone lifecycle.

Every rule here is mechanical and stated in full, because an order block is
only as trustworthy as the rule that produced it.

A **bullish** order block requires all three, in this order:

1. A *displacement* candle: an up-close candle whose body is at least
   ``displacement_atr`` x Wilder ATR(14) measured at that bar.
2. An *origin* candle: the most recent down-close candle before it, within
   ``origin_lookback`` bars. Its full high-low range is the zone.
3. A *break of structure*: the displacement leg must close above the most
   recent confirmed swing high that formed before the origin candle.

A **bearish** order block is the exact mirror.

Two properties matter for correctness:

* **No look-ahead.** Swing pivots need ``strength`` bars on both sides, and a
  zone is only emitted once its displacement candle has closed. A zone is
  therefore dated by its origin candle but *known* only from the displacement
  candle onward, and `detect` never consults a bar past `as_of`.
* **No duplicates.** A zone's identity is
  ``(symbol, timeframe, side, origin_ts)``, which is stable across restarts
  and re-scans, so persisting the same scan twice cannot create two rows.

The `score` is a **descriptive ranking** of how cleanly a zone met these
rules. It is not a win rate, not a probability, and not a prediction.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from market.indicators import ATR_PERIOD, swing_pivots, wilder_atr
from market.store import Candle


@dataclass(frozen=True)
class OBConfig:
    """Versioned so a stored zone always records the rules that produced it."""
    version: str = "ob-v1"
    displacement_atr: float = 1.0
    origin_lookback: int = 5
    pivot_strength: int = 2
    max_age_bars: int = 400
    # Fraction of the zone that must be traded through to count as mitigated.
    mitigation_fraction: float = 0.5

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "displacement_atr": self.displacement_atr,
            "origin_lookback": self.origin_lookback,
            "pivot_strength": self.pivot_strength,
            "max_age_bars": self.max_age_bars,
            "mitigation_fraction": self.mitigation_fraction,
        }


DEFAULT_CONFIG = OBConfig()

FRESH, TAPPED, MITIGATED, INVALIDATED, EXPIRED = (
    "FRESH", "TAPPED", "MITIGATED", "INVALIDATED", "EXPIRED",
)


@dataclass
class ZoneEvent:
    """A state change in a zone's life. Keyed so replays cannot duplicate it."""
    zone_id: str
    symbol: str
    timeframe: str
    kind: str          # TAPPED | MITIGATED | INVALIDATED | EXPIRED
    ts: str
    price: float

    @property
    def event_id(self) -> str:
        return _digest(self.zone_id, self.kind, self.ts)

    def as_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "zone_id": self.zone_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "kind": self.kind,
            "ts": self.ts,
            "price": self.price,
        }


@dataclass
class OrderBlock:
    zone_id: str
    symbol: str
    timeframe: str
    side: str              # BULLISH | BEARISH
    zone_low: float
    zone_high: float
    origin_ts: str         # the origin candle -- how the zone is dated
    confirmed_ts: str      # the displacement candle -- when it became known
    displacement_atr: float
    bos_level: float
    status: str
    age_bars: int
    # Distance from the current price to the nearest edge, in ATR units.
    # 0.0 means price is inside the zone.
    distance_atr: float | None
    distance_price: float | None
    score: float
    score_basis: str
    config_version: str
    events: list[ZoneEvent] = field(default_factory=list)

    @property
    def midpoint(self) -> float:
        return (self.zone_low + self.zone_high) / 2.0

    def as_dict(self) -> dict:
        return {
            "zone_id": self.zone_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "side": self.side,
            "zone_low": self.zone_low,
            "zone_high": self.zone_high,
            "midpoint": self.midpoint,
            "origin_ts": self.origin_ts,
            "confirmed_ts": self.confirmed_ts,
            "displacement_atr": round(self.displacement_atr, 3),
            "bos_level": self.bos_level,
            "status": self.status,
            "age_bars": self.age_bars,
            "distance_atr": round(self.distance_atr, 3) if self.distance_atr is not None else None,
            "distance_price": self.distance_price,
            "score": round(self.score, 1),
            "score_basis": self.score_basis,
            "config_version": self.config_version,
            "events": [e.as_dict() for e in self.events],
        }


_SCORE_BASIS = (
    "Descriptive ranking of how cleanly the zone met the detection rules "
    "(displacement size, zone tightness, freshness). Not a win rate and not a "
    "probability."
)


def _digest(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _score(*, displacement_atr: float, zone_atr: float, status: str, age_bars: int,
           max_age: int) -> float:
    """0-100 descriptive ranking. Bigger displacement and a tighter zone rank
    higher; a zone already traded through ranks lower."""
    disp = min(displacement_atr / 3.0, 1.0) * 45.0
    tightness = (1.0 - min(zone_atr / 2.0, 1.0)) * 25.0 if zone_atr > 0 else 0.0
    freshness = max(0.0, 1.0 - age_bars / max(max_age, 1)) * 15.0
    state = {FRESH: 15.0, TAPPED: 9.0, MITIGATED: 4.0, EXPIRED: 1.0, INVALIDATED: 0.0}[status]
    return round(disp + tightness + freshness + state, 1)


def _last_pivot_before(pivots: list, index: int, kind: str) -> float | None:
    """Most recent confirmed pivot of `kind` strictly before `index`."""
    best = None
    for p in pivots:
        if p.index < index and p.kind == kind:
            best = p.price
    return best


def detect(
    candles: list[Candle],
    *,
    symbol: str,
    timeframe: str,
    config: OBConfig = DEFAULT_CONFIG,
) -> list[OrderBlock]:
    """Detect every order block visible in `candles` and resolve its lifecycle.

    `candles` must already be truncated to the as-of moment by the caller; this
    function reads nothing beyond the list it is given.
    """
    n = len(candles)
    if n < ATR_PERIOD + config.origin_lookback + 2 * config.pivot_strength + 2:
        return []

    atr = wilder_atr(candles, ATR_PERIOD)
    pivots = swing_pivots(candles, strength=config.pivot_strength)
    current = candles[-1].close
    current_atr = next((v for v in reversed(atr) if v is not None), None)

    zones: list[OrderBlock] = []
    seen: set[str] = set()

    for i in range(ATR_PERIOD + 1, n):
        bar, bar_atr = candles[i], atr[i]
        if bar_atr is None or bar_atr <= 0:
            continue
        body = bar.close - bar.open
        if abs(body) < config.displacement_atr * bar_atr:
            continue

        bullish = body > 0
        # The origin candle: nearest opposite-close candle behind the
        # displacement, within the configured lookback.
        origin_idx = None
        for j in range(i - 1, max(i - 1 - config.origin_lookback, -1), -1):
            opposite = candles[j].close < candles[j].open if bullish else candles[j].close > candles[j].open
            if opposite:
                origin_idx = j
                break
        if origin_idx is None:
            continue

        # Break of structure against the last opposing pivot before the origin.
        bos_level = _last_pivot_before(pivots, origin_idx, "HIGH" if bullish else "LOW")
        if bos_level is None:
            continue
        if bullish and bar.close <= bos_level:
            continue
        if not bullish and bar.close >= bos_level:
            continue

        origin = candles[origin_idx]
        zone_low, zone_high = origin.low, origin.high
        if zone_high <= zone_low:
            continue

        side = "BULLISH" if bullish else "BEARISH"
        zone_id = _digest(symbol, timeframe, side, origin.ts.isoformat())
        if zone_id in seen:
            continue
        seen.add(zone_id)

        status, events = _resolve_lifecycle(
            candles, i, zone_low, zone_high, bullish, symbol, timeframe, zone_id, config
        )
        age_bars = n - 1 - i

        dist_price: float | None = None
        dist_atr: float | None = None
        if current_atr and current_atr > 0:
            if zone_low <= current <= zone_high:
                dist_price, dist_atr = 0.0, 0.0
            else:
                dist_price = (zone_low - current) if current < zone_low else (current - zone_high)
                dist_atr = dist_price / current_atr

        zones.append(
            OrderBlock(
                zone_id=zone_id,
                symbol=symbol,
                timeframe=timeframe,
                side=side,
                zone_low=zone_low,
                zone_high=zone_high,
                origin_ts=origin.ts.isoformat(),
                confirmed_ts=bar.ts.isoformat(),
                displacement_atr=abs(body) / bar_atr,
                bos_level=bos_level,
                status=status,
                age_bars=age_bars,
                distance_atr=dist_atr,
                distance_price=dist_price,
                score=_score(
                    displacement_atr=abs(body) / bar_atr,
                    zone_atr=(zone_high - zone_low) / bar_atr,
                    status=status,
                    age_bars=age_bars,
                    max_age=config.max_age_bars,
                ),
                score_basis=_SCORE_BASIS,
                config_version=config.version,
                events=events,
            )
        )

    return zones


def _resolve_lifecycle(
    candles: list[Candle],
    confirm_idx: int,
    zone_low: float,
    zone_high: float,
    bullish: bool,
    symbol: str,
    timeframe: str,
    zone_id: str,
    config: OBConfig,
) -> tuple[str, list[ZoneEvent]]:
    """Walk forward from the displacement candle, recording each first-time
    transition. Terminal states stop the walk."""
    mid = zone_low + (zone_high - zone_low) * (
        1.0 - config.mitigation_fraction if bullish else config.mitigation_fraction
    )
    status = FRESH
    events: list[ZoneEvent] = []

    def add(kind: str, candle: Candle, price: float) -> None:
        events.append(
            ZoneEvent(
                zone_id=zone_id, symbol=symbol, timeframe=timeframe,
                kind=kind, ts=candle.ts.isoformat(), price=price,
            )
        )

    for k in range(confirm_idx + 1, len(candles)):
        candle = candles[k]
        # Invalidation first: a close beyond the far side ends the zone.
        if (bullish and candle.close < zone_low) or (not bullish and candle.close > zone_high):
            add(INVALIDATED, candle, candle.close)
            return INVALIDATED, events

        touched = candle.low <= zone_high and candle.high >= zone_low
        if touched and status == FRESH:
            status = TAPPED
            add(TAPPED, candle, zone_high if bullish else zone_low)
        if status == TAPPED and ((bullish and candle.low <= mid) or (not bullish and candle.high >= mid)):
            status = MITIGATED
            add(MITIGATED, candle, mid)

        if k - confirm_idx > config.max_age_bars:
            add(EXPIRED, candle, candle.close)
            return EXPIRED, events

    return status, events


def nearest_zone(zones: list[OrderBlock], *, side: str | None = None) -> OrderBlock | None:
    """The closest still-actionable zone to current price. Invalidated and
    expired zones are excluded -- they are history, not levels."""
    live = [
        (z.distance_atr, z) for z in zones
        if z.status in (FRESH, TAPPED, MITIGATED)
        and z.distance_atr is not None
        and (side is None or z.side == side)
    ]
    return min(live, key=lambda pair: pair[0] or 0.0)[1] if live else None
