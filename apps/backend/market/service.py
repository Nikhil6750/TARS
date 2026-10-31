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

Two things here exist to keep that true once a real feed is attached
(P2-C3). `_replay_pinned` records that the user has deliberately moved the
cursor off the live edge, and every status decision goes through
`clock.resolve_status`, which cannot return LIVE while that pin is set. And
every payload carries `provenance`, naming the source of the observations
in it, so a client or a test can check what it is looking at instead of
inferring it from a label.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from market import clock, orderblocks, provenance, strength, universe, xray
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
    # v2: reads stop at the last *closed* bar. v1 included the bar still
    # forming at the cursor, so replayed numbers differ between the two.
    version: str = "xray-v2"
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
        # Set only by a real feed adapter via `attach_feed`; stays None while
        # no feed is authenticated, which is what keeps the status out of LIVE.
        self._feed_last_tick: datetime | None = None
        self._feed_provider: str = provenance.NO_PROVIDER
        self._feed_state: dict | None = None
        self._feed_connection: str = "NOT_CONFIGURED"
        self._feed_detail: str = ""
        # The close of the newest live bar every instrument has reached. Until
        # this exists the panels are recorded history, whatever the ticks say.
        self._live_edge: datetime | None = None
        # True once the cursor has been moved off the live edge on purpose.
        # While it is set, no status can be LIVE -- see `clock.resolve_status`.
        # It starts False: with no feed attached the status is REPLAY anyway,
        # and pinning on construction would be a claim the user never made.
        self._replay_pinned: bool = False

        self.state.record_config(self.config.version, self.config.as_dict())

    # ---- cursor ----------------------------------------------------------
    @property
    def panels_follow_feed(self) -> bool:
        """Whether the panels are being computed from the feed's own bars."""
        return (
            not self._replay_pinned
            and self._live_edge is not None
            and self._feed_last_tick is not None
        )

    @property
    def cursor(self) -> datetime:
        if self.panels_follow_feed and self._live_edge is not None:
            return self._live_edge
        if self._cursor is None:
            raise RuntimeError("candle store is empty; run `python -m market.ingest` first")
        return self._cursor

    @property
    def bounds(self) -> tuple[datetime | None, datetime | None]:
        return self._first_bar, self._last_bar

    def set_cursor(self, moment: datetime, *, pin_replay: bool = True) -> datetime:
        """Move the as-of cursor, clamped to the store's real coverage so the
        dashboard can never be pointed at a moment it has no data for.

        Moving the cursor pins replay by default: the caller has chosen a
        moment, so what is shown is recorded history and must not be
        labelled live even if a feed is connected. `reset_cursor` is the one
        caller that unpins, because returning to the live edge is the
        request to stop replaying.
        """
        with self._lock:
            if self._first_bar is None or self._last_bar is None:
                raise RuntimeError("candle store is empty")
            target = moment.astimezone(UTC)
            self._cursor = max(self._first_bar, min(self._last_bar, target))
            if pin_replay:
                self._replay_pinned = True
            return self._cursor

    def step_cursor(self, *, timeframe: str, bars: int) -> datetime:
        minutes = universe.TIMEFRAMES.get(timeframe.upper())
        if minutes is None:
            raise ValueError("unknown timeframe " + timeframe)
        return self.set_cursor(self.cursor + timedelta(minutes=minutes * bars))

    def reset_cursor(self) -> datetime:
        """Back to the newest bar, and stop replaying."""
        if self._last_bar is None:
            raise RuntimeError("candle store is empty")
        cursor = self.set_cursor(self._last_bar, pin_replay=False)
        with self._lock:
            self._replay_pinned = False
        return cursor

    @property
    def replay_pinned(self) -> bool:
        return self._replay_pinned

    # ---- feed attachment -------------------------------------------------
    def attach_feed(self, *, provider: str, last_tick: datetime | None) -> None:
        """Report the newest tick from an authenticated provider.

        The seam a real adapter uses (P2-C1) so that wiring a feed never
        requires reaching into this object's internals. Attaching a feed is
        not on its own enough to produce LIVE: the replay pin still wins,
        and the tick still has to be fresh and the market open.
        """
        if not provider or provider == provenance.NO_PROVIDER:
            raise ValueError("a live feed must name its provider")
        with self._lock:
            self._feed_provider = provider
            self._feed_last_tick = last_tick.astimezone(UTC) if last_tick else None

    def detach_feed(self) -> None:
        """Forget the feed. The dashboard falls back to recorded history."""
        with self._lock:
            self._feed_provider = provenance.NO_PROVIDER
            self._feed_last_tick = None
            self._live_edge = None
            self._feed_connection = "NOT_CONFIGURED"
            self._feed_detail = ""
            self._feed_state = None

    def report_feed(self, state, *, last_tick: datetime | None, connected: bool) -> None:
        """One pass of the feed bridge. A dropped connection keeps the last
        tick, so the status ages into STALE and says so, rather than quietly
        falling back to history as if no feed had ever been there."""
        with self._lock:
            self._feed_connection = state.connection.value
            self._feed_detail = state.detail
            self._feed_state = state.as_dict()
        if connected and state.provider:
            self.attach_feed(provider=state.provider, last_tick=last_tick)

    def report_live_edge(self, edge: datetime) -> None:
        with self._lock:
            self._live_edge = edge.astimezone(UTC)

    @property
    def feed_state(self) -> dict | None:
        return self._feed_state

    def adopt_feed_from(self, previous: MarketXRayService) -> None:
        """Carry the live side across a store reload after an import."""
        with self._lock:
            self._feed_provider = previous._feed_provider
            self._feed_last_tick = previous._feed_last_tick
            self._feed_state = previous._feed_state
            self._feed_connection = previous._feed_connection
            self._feed_detail = previous._feed_detail

    def data_provenance(self) -> provenance.Provenance:
        """Where the observations in the next payload come from.

        Derived from the same inputs as the status, so the two can never
        disagree: anything but a resolved LIVE/STALE feed is recorded data.
        """
        status = self._resolve_status()
        if status.source == provenance.DataSource.LIVE_FEED.value:
            return provenance.live(
                provider=self._feed_provider,
                observed_at=status.data_as_of,
                fresh=status.status is clock.FeedStatus.LIVE,
            )
        detail = ""
        if self._feed_last_tick is not None and self._replay_pinned:
            detail = (
                "A live feed is attached but the cursor is pinned to recorded "
                "history, so these observations are replayed, not live."
            )
        elif self._feed_last_tick is not None:
            detail = (
                "A live feed is connected but has not delivered bars yet, so "
                "these observations are recorded history, not live."
            )
        return provenance.recorded(observed_at=status.data_as_of, detail=detail)

    # ---- status ----------------------------------------------------------
    def market_status(self) -> clock.MarketStatus:
        """The status, recorded to the feed-health history.

        `_resolve_status` is the same decision without the write, so
        stamping provenance on a payload does not persist a health row for
        every panel it contains.
        """
        status = self._resolve_status()
        self.state.record_feed_health(status)
        return status

    def _resolve_status(self) -> clock.MarketStatus:
        now = datetime.now(UTC)
        status = clock.resolve_status(
            now=now,
            feed_last_tick=self._feed_last_tick,
            replay_pinned=self._replay_pinned,
            panels_follow_feed=self.panels_follow_feed,
            cursor=self._cursor,
            store_last_bar=self._last_bar,
            feed_connection=self._feed_connection,
            feed_detail=self._feed_detail,
        )
        # Fail closed. The gate above cannot return LIVE while replay is
        # pinned, so this can only fire if that guarantee is ever broken --
        # in which case showing REPLAY and logging loudly is the right
        # outcome, not surfacing a label we know to be wrong.
        if status.status is clock.FeedStatus.LIVE and self._replay_pinned:
            logger.error(
                "feed status resolved LIVE while the replay cursor is pinned; "
                "downgrading to REPLAY (this is a bug in clock.resolve_status)"
            )
            status = clock.replay_status(
                now=now, cursor=self.cursor, store_last_bar=self._last_bar
            )
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
                "backing": meta.backing,
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
        payload = snap.as_dict()
        payload["provenance"] = self.data_provenance().as_dict()
        return payload

    def strength(self, *, window: str | None = None) -> dict:
        snap = strength.compute_strength(
            self.store, as_of=self.cursor, window=window or self.config.strength_window
        )
        self.state.record_strength(snap)
        payload = snap.as_dict()
        payload["provenance"] = self.data_provenance().as_dict()
        return payload

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
            "provenance": self.data_provenance().as_dict(),
        }

    def candles(self, *, symbol: str, timeframe: str, limit: int = CHART_BARS) -> dict:
        symbol = symbol.upper()
        candles = self.store.candles(symbol, timeframe, as_of=self.cursor, limit=limit)
        if not candles:
            return {
                "symbol": symbol, "timeframe": timeframe, "as_of": self.cursor.isoformat(),
                "candles": [], "error": "no completed bars at or before the current cursor",
                "provenance": self.data_provenance().as_dict(),
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
            "provenance": self.data_provenance().as_dict(),
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
            "provenance": self.data_provenance().as_dict(),
        }

    def events(self, *, symbol: str | None = None, limit: int = 100) -> dict:
        return {
            "events": self.state.recent_events(symbol=symbol, limit=limit),
            "total_stored": self.state.event_count(),
            "zones_stored": self.state.zone_count(),
            "provenance": self.data_provenance().as_dict(),
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
            "provenance": self.data_provenance().as_dict(),
            "replay_pinned": self._replay_pinned,
            "feed": self._feed_state,
        }
