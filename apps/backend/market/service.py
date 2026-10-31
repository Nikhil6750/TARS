"""MarketXRayService -- the backend's single source of truth for the dashboard.

Holds the as-of cursor, computes every panel from the candle store, and
persists zones, events, strength history and feed health. The frontend renders
what this returns and calculates nothing of its own.

The cursor is the backbone. Because this machine has recorded history ending
2026-09-14 and no authenticated live feed, the dashboard runs in REPLAY: the
cursor names the moment being shown and every panel is computed with
``ts <= cursor``. Advancing or rewinding the cursor changes what is shown
deterministically, and no panel can ever be labelled LIVE from it (see
`market.clock`).
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from market import clock, orderblocks, strength, universe, xray
from market.indicators import normalized_return, volatility_regime
from market.orderblocks import OBConfig
from market.state import MarketState
from market.store import CandleStore

logger = logging.getLogger("tars.market.service")

CHART_BARS = 300
OB_SCAN_BARS = 600
LEADERS = 6


@dataclass
class CalcConfig:
    """Everything that affects a number the dashboard shows, versioned
    together so a stored zone or snapshot can be traced to its rules."""
    version: str = "xray-v1"
    xray_return_window: int = xray.DEFAULT_RETURN_WINDOW
    xray_points: int = xray.DEFAULT_POINTS
    strength_window: str = strength.DEFAULT_WINDOW
    ob: OBConfig = OBConfig()

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "xray_return_window": self.xray_return_window,
            "xray_points": self.xray_points,
            "strength_window": self.strength_window,
            "order_blocks": self.ob.as_dict(),
            "atr_period": 14,
            "timeframes": universe.TIMEFRAMES,
        }


class MarketXRayService:
    def __init__(
        self,
        store: CandleStore | None = None,
        state: MarketState | None = None,
        config: CalcConfig | None = None,
    ):
        self.store = store or CandleStore()
        self.state = state or MarketState()
        self.config = config or CalcConfig()
        self._lock = threading.Lock()

        self._first_bar, self._last_bar = self.store.store_bounds("M5")
        self._cursor: datetime | None = self._last_bar
        # Set only by a real feed adapter; stays None while no feed is
        # authenticated, which is what keeps the status out of LIVE.
        self._feed_last_tick: datetime | None = None

        self.state.record_config(self.config.version, self.config.as_dict())

    # ---- cursor ----------------------------------------------------------
    @property
    def cursor(self) -> datetime:
        if self._cursor is None:
            raise RuntimeError("candle store is empty; run `python -m market.ingest` first")
        return self._cursor

    @property
    def bounds(self) -> tuple[datetime | None, datetime | None]:
        return self._first_bar, self._last_bar

    def set_cursor(self, moment: datetime) -> datetime:
        """Move the as-of cursor, clamped to the store's real coverage so the
        dashboard can never be pointed at a moment it has no data for."""
        with self._lock:
            if self._first_bar is None or self._last_bar is None:
                raise RuntimeError("candle store is empty")
            target = moment.astimezone(UTC)
            self._cursor = max(self._first_bar, min(self._last_bar, target))
            return self._cursor

    def step_cursor(self, *, timeframe: str, bars: int) -> datetime:
        minutes = universe.TIMEFRAMES.get(timeframe.upper())
        if minutes is None:
            raise ValueError("unknown timeframe " + timeframe)
        return self.set_cursor(self.cursor + timedelta(minutes=minutes * bars))

    def reset_cursor(self) -> datetime:
        if self._last_bar is None:
            raise RuntimeError("candle store is empty")
        return self.set_cursor(self._last_bar)

    # ---- status ----------------------------------------------------------
    def market_status(self) -> clock.MarketStatus:
        now = datetime.now(UTC)
        if self._feed_last_tick is not None:
            status = clock.live_status(now=now, feed_last_tick=self._feed_last_tick)
        elif self._cursor is None:
            status = clock.MarketStatus(
                status=clock.FeedStatus.NO_DATA, market_open=clock.is_market_open(now),
                as_of=now, data_as_of=None, data_age_seconds=None,
                sessions_active=clock.active_sessions(now),
                next_transition=clock.next_transition(now),
                detail="No candle store. Run `python -m market.ingest`.", replay=False,
            )
        else:
            status = clock.replay_status(
                now=now, cursor=self._cursor, store_last_bar=self._last_bar
            )
        self.state.record_feed_health(status)
        return status

    # ---- panels ----------------------------------------------------------
    def instruments(self) -> list[dict]:
        out = []
        for symbol in self.store.available_symbols:
            meta = universe.INSTRUMENTS[symbol]
            cov = self.store.coverage(symbol, "M5")
            out.append({
                "symbol": symbol,
                "asset_class": meta.asset_class,
                "base": meta.base,
                "quote": meta.quote,
                "price_precision": meta.price_precision,
                "pip_size": meta.pip_size,
                "bars_m5": cov.bars,
                "first_bar": cov.first_bar.isoformat() if cov.first_bar else None,
                "last_bar": cov.last_bar.isoformat() if cov.last_bar else None,
            })
        return out

    def xray(self, *, timeframe: str) -> dict:
        snap = xray.build_xray(
            self.store, as_of=self.cursor, timeframe=timeframe,
            return_window=self.config.xray_return_window,
            points=self.config.xray_points,
        )
        self.state.record_snapshots([
            (s.symbol, timeframe, snap.as_of, s.current_close, s.pct_change, s.current_z,
             None, None)
            for s in snap.series
        ])
        return snap.as_dict()

    def strength(self, *, window: str | None = None) -> dict:
        snap = strength.compute_strength(
            self.store, as_of=self.cursor, window=window or self.config.strength_window
        )
        self.state.record_strength(snap)
        return snap.as_dict()

    def leaders(self, *, timeframe: str, limit: int = LEADERS) -> dict:
        """Ranked bullish and bearish cards, each carrying its real price, its
        normalised strength, its volatility regime and the distance to its
        nearest qualifying order block."""
        snap = xray.build_xray(
            self.store, as_of=self.cursor, timeframe=timeframe,
            return_window=self.config.xray_return_window, points=2,
        )
        cards: list[dict] = []
        for s in snap.series:
            candles = self.store.candles(s.symbol, timeframe, as_of=self.cursor, limit=OB_SCAN_BARS)
            regime = volatility_regime(candles)
            zones = orderblocks.detect(
                candles, symbol=s.symbol, timeframe=timeframe, config=self.config.ob
            )
            side = "BULLISH" if s.current_z >= 0 else "BEARISH"
            near = orderblocks.nearest_zone(zones, side=side) or orderblocks.nearest_zone(zones)
            cards.append(
                xray.LeaderCard(
                    symbol=s.symbol,
                    asset_class=s.asset_class,
                    close=s.current_close,
                    pct_change=s.pct_change,
                    z=s.current_z,
                    rank=s.rank,
                    direction=s.direction,
                    atr=round(regime.atr, 6) if regime.atr else None,
                    regime=regime.label,
                    spread=round(candles[-1].spread, 6) if candles else None,
                    nearest_zone=(
                        {
                            "zone_id": near.zone_id,
                            "side": near.side,
                            "status": near.status,
                            "zone_low": near.zone_low,
                            "zone_high": near.zone_high,
                            "distance_atr": round(near.distance_atr, 3)
                            if near.distance_atr is not None else None,
                            "score": near.score,
                        }
                        if near else None
                    ),
                ).as_dict()
            )

        bullish = [c for c in cards if c["z"] > 0][:limit]
        bearish = sorted([c for c in cards if c["z"] < 0], key=lambda c: c["z"])[:limit]
        return {
            "timeframe": timeframe,
            "as_of": snap.as_of,
            "bullish": bullish,
            "bearish": bearish,
            "measures": snap.measures,
        }

    def candles(self, *, symbol: str, timeframe: str, limit: int = CHART_BARS) -> dict:
        symbol = symbol.upper()
        candles = self.store.candles(symbol, timeframe, as_of=self.cursor, limit=limit)
        if not candles:
            return {
                "symbol": symbol, "timeframe": timeframe, "as_of": self.cursor.isoformat(),
                "candles": [], "error": "no completed bars at or before the current cursor",
            }
        regime = volatility_regime(candles)
        closes = [c.close for c in candles]
        nr = normalized_return(closes, window=self.config.xray_return_window)
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "as_of": self.cursor.isoformat(),
            "candles": [c.as_dict() for c in candles],
            "regime": regime.as_dict(),
            "normalized_return": nr.as_dict() if nr else None,
            "precision": universe.INSTRUMENTS[symbol].price_precision
            if symbol in universe.INSTRUMENTS else 5,
        }

    def order_blocks(self, *, symbol: str, timeframe: str) -> dict:
        """Detect, persist and return the zones for one instrument.

        Persisting here is deliberate: the same scan both answers the request
        and records the zone/event history, and because both are keyed on
        content it is safe to call repeatedly.
        """
        symbol = symbol.upper()
        candles = self.store.candles(symbol, timeframe, as_of=self.cursor, limit=OB_SCAN_BARS)
        zones = orderblocks.detect(
            candles, symbol=symbol, timeframe=timeframe, config=self.config.ob
        )
        events = [e for z in zones for e in z.events]
        self.state.upsert_zones(zones)
        new_events = self.state.record_events(events)

        current = candles[-1].close if candles else None
        active = [z for z in zones if z.status in ("FRESH", "TAPPED", "MITIGATED")]
        near = orderblocks.nearest_zone(zones)
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "as_of": self.cursor.isoformat(),
            "current_price": current,
            "zones": [z.as_dict() for z in zones],
            "active_count": len(active),
            "nearest": near.as_dict() if near else None,
            "events_recorded_new": new_events,
            "config": self.config.ob.as_dict(),
        }

    def events(self, *, symbol: str | None = None, limit: int = 100) -> dict:
        return {
            "events": self.state.recent_events(symbol=symbol, limit=limit),
            "total_stored": self.state.event_count(),
            "zones_stored": self.state.zone_count(),
        }

    # ---- aggregate -------------------------------------------------------
    def dashboard(self, *, timeframe: str = universe.DEFAULT_TIMEFRAME,
                  symbol: str | None = None, strength_window: str | None = None) -> dict:
        """One payload with everything the dashboard needs for a first paint."""
        timeframe = timeframe.upper()
        if timeframe not in universe.TIMEFRAMES:
            raise ValueError("unknown timeframe " + timeframe)
        available = self.store.available_symbols
        selected = (symbol or ("XAUUSD" if "XAUUSD" in available else available[0])).upper()

        return {
            "status": self.market_status().as_dict(),
            "cursor": {
                "as_of": self.cursor.isoformat(),
                "first_bar": self._first_bar.isoformat() if self._first_bar else None,
                "last_bar": self._last_bar.isoformat() if self._last_bar else None,
            },
            "timeframe": timeframe,
            "timeframes": list(universe.TIMEFRAMES),
            "selected_symbol": selected,
            "instruments": self.instruments(),
            "xray": self.xray(timeframe=timeframe),
            "strength": self.strength(window=strength_window),
            "leaders": self.leaders(timeframe=timeframe),
            "chart": self.candles(symbol=selected, timeframe=timeframe),
            "order_blocks": self.order_blocks(symbol=selected, timeframe=timeframe),
            "events": self.events(limit=40),
            "config": self.config.as_dict(),
        }
