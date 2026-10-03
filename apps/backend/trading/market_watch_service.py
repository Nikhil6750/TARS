"""MarketWatchService -- the local TARS watcher (mission: TARS Watcher
Intelligence). NOT Claude: this module's whole job is deciding, with plain
deterministic logic, whether a tick of provider state is worth anyone's
attention at all, and -- only when correlated evidence genuinely justifies
it -- escalating to exactly ONE Claude call through the existing
RealtimeEventCore/EventAnalysisAgent pipeline (events/core.py). It never
calls an LLM directly.

Composes existing infrastructure rather than duplicating it:
- MT5Provider/CalendarMonitor/NewsMonitor already detect and publish raw
  price moves, spread spikes, position changes, upcoming/released calendar
  items and relevant headlines as NormalizedEvents (see monitors/*.py) --
  this service does not re-detect any of that.
- RealtimeEventCore already provides persistence, admission dedupe+cooldown,
  the significance gate, and the one Claude-escalation path (ANALYZE ->
  EventAnalysisAgent -> orchestrator.analyze_monitor_event) -- this service
  publishes through it (`core.publish`), it does not reimplement it.
- SignificanceGate.allow_symbol(symbol, analyze=False) (events/core.py) is
  what makes a newly-watched symbol's raw single-source events visible as
  deterministic NOTIFY alerts without letting any one of them independently
  trigger Claude -- only this service's own correlated SYSTEM-sourced
  synthesis (exempt from that cap, see SignificanceGate.decide) can do that.

What IS new here: watching an arbitrary user-chosen symbol at all (dynamic,
persisted, not the static `event_relevant_symbols` CSV), correlating that
symbol's recent price/calendar/news evidence into ONE alert instead of three,
an explicit `requires_reasoning()` gate deciding whether that correlated
story is worth Claude's attention, and a watcher-specific, configurable
dedupe/cooldown layer on top of the core's (so "don't repeat the same story
every 30s" and "do repeat it if something genuinely new happened" both work
over a realistic multi-minute/hour horizon, not just the core's short
admission window).
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from events.core import EventSource, NormalizedEvent

logger = logging.getLogger("tars.trading.market_watch")

# Conservative defaults (mission section 5/20): configurable, never presented
# as a proven trading edge, just "is this move unusual relative to this
# symbol's own recent behavior." A move must beat BOTH a floor (so a dead-
# quiet symbol's tiny wobbles never count) and a multiple of its own recent
# average absolute change (so a genuinely volatile symbol isn't flagged on
# every ordinary tick).
DEFAULT_MIN_PRICE_SAMPLES = 5
DEFAULT_FALLBACK_ABS_MOVE_PCT = 0.3
DEFAULT_VOLATILITY_MULTIPLIER = 3.0
DEFAULT_CORRELATION_WINDOW_SECONDS = 30 * 60.0
DEFAULT_COOLDOWN_SECONDS = 30 * 60.0
DEFAULT_INTERVAL_SECONDS = 30.0

_CALENDAR_KINDS = {"calendar.upcoming", "calendar.released"}
_PRICE_KINDS = {"price.move", "spread.spike"}


@dataclass
class EventCandidate:
    """One tick's gathered evidence for one watched symbol -- the unit
    `requires_reasoning()` decides over."""

    symbol: str
    has_price_move: bool = False
    move_pct: float | None = None
    move_is_unusual_without_obvious_cause: bool = False
    insufficient_price_history: bool = False
    has_calendar_event: bool = False
    calendar_title: str = ""
    calendar_event_id: str | None = None
    has_relevant_news: bool = False
    news_headlines: tuple[str, ...] = ()
    has_open_position: bool = False
    # No sentiment/direction signal exists anywhere in this codebase's
    # evidence (news/calendar carry no polarity) -- always False rather
    # than fabricating a conflict-detection heuristic with nothing real
    # behind it. A future news/calendar source with directional signal
    # could make this real.
    has_conflicting_catalysts: bool = False
    evidence_ids: tuple[str, ...] = ()

    @property
    def factor_count(self) -> int:
        return sum([self.has_price_move, self.has_calendar_event, self.has_relevant_news])


def requires_reasoning(candidate: EventCandidate) -> bool:
    """Deterministic Claude-escalation gate (mission section 11). Kept as
    a pure function, independent of MarketWatchService's own state, so it
    is directly testable against hand-built candidates."""
    if candidate.factor_count >= 2:
        return True
    if candidate.has_price_move and candidate.move_is_unusual_without_obvious_cause:
        return True
    if candidate.has_conflicting_catalysts:
        return True
    if candidate.has_open_position and candidate.has_calendar_event:
        return True
    return False


def _causality_class(candidate: EventCandidate) -> str:
    """Evidence-class discipline (mission section 9) -- correlation is
    never claimed as proof of cause; language elsewhere (the alert
    title/summary) must hedge accordingly for anything short of
    STRONG_TEMPORAL."""
    if candidate.has_calendar_event and candidate.has_price_move:
        return "STRONG_TEMPORAL"
    if (candidate.has_relevant_news or candidate.has_calendar_event) and candidate.has_price_move:
        return "PLAUSIBLE"
    return "INSUFFICIENT"


@dataclass
class _EvidenceItem:
    kind: str  # "price" | "calendar" | "news"
    at: datetime
    detail: dict = field(default_factory=dict)
    evidence_id: str = ""


class MarketWatchService:
    def __init__(
        self,
        watchlist_store,
        asset_resolver,
        monitors,
        core,
        gate,
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        correlation_window_seconds: float = DEFAULT_CORRELATION_WINDOW_SECONDS,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        volatility_multiplier: float = DEFAULT_VOLATILITY_MULTIPLIER,
        min_price_samples: int = DEFAULT_MIN_PRICE_SAMPLES,
        fallback_abs_move_pct: float = DEFAULT_FALLBACK_ABS_MOVE_PCT,
        app_state_store=None,
        clock=None,
    ) -> None:
        self._watchlist = watchlist_store
        self._resolver = asset_resolver
        self._monitors = monitors
        self._core = core
        self._gate = gate
        self._app_state = app_state_store
        self.interval_seconds = interval_seconds
        self.correlation_window_seconds = correlation_window_seconds
        self.cooldown_seconds = cooldown_seconds
        self.volatility_multiplier = volatility_multiplier
        self.min_price_samples = min_price_samples
        self.fallback_abs_move_pct = fallback_abs_move_pct
        self._clock = clock or (lambda: datetime.now(UTC))

        self._paused = False
        self._added_provider_symbols: set[str] = set()  # symbols THIS service added to providers' lists
        self._price_history: dict[str, deque[tuple[datetime, float]]] = {}
        self._evidence: dict[str, deque[_EvidenceItem]] = {}
        self._last_published: dict[str, tuple[datetime, tuple[str, ...]]] = {}  # symbol -> (at, signature)
        self._unsubscribe = None
        self._task = None

        # Observability (mission acceptance test I).
        self.cycles = 0
        self.events_considered = 0
        self.alerts_published = 0

    # ---- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if self._app_state is not None:
            paused_raw = await self._app_state.get("watcher_paused")
            self._paused = paused_raw == "true"
        for entry in await self._watchlist.load():
            self._admit_symbol(entry.canonical_symbol)
        self._unsubscribe = self._core.subscribe(self._on_event)
        import asyncio

        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        if self._task:
            self._task.cancel()
            import asyncio

            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        import asyncio

        while True:
            # Sleep BEFORE the first tick, not after: a just-started watcher
            # has nothing new to assess yet, and callers (including tests)
            # that drive `tick()` explicitly must get deterministic counts
            # without racing this background loop's own first iteration.
            await asyncio.sleep(self.interval_seconds)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("market watch tick failed; watcher remains alive")

    # ---- watchlist control (voice tools call these) -----------------------

    async def watch(self, asset: str, *, active_symbol: str | None = None):
        resolved = self._resolver.resolve(asset, active_symbol=active_symbol)
        if resolved.outcome != "RESOLVED":
            return resolved
        symbol = resolved.symbol
        assert symbol is not None
        entry = await self._watchlist.add(asset, symbol)
        self._admit_symbol(symbol)
        return resolved, entry

    async def unwatch(self, asset: str, *, active_symbol: str | None = None):
        resolved = self._resolver.resolve(asset, active_symbol=active_symbol)
        if resolved.outcome != "RESOLVED":
            return resolved
        symbol = resolved.symbol
        assert symbol is not None
        removed = await self._watchlist.remove(symbol)
        if removed:
            self._dismiss_symbol(symbol)
        return resolved, removed

    async def list_watched(self):
        return await self._watchlist.load()

    async def pause(self) -> None:
        self._paused = True
        if self._app_state is not None:
            await self._app_state.set("watcher_paused", "true")

    async def resume(self) -> None:
        self._paused = False
        if self._app_state is not None:
            await self._app_state.set("watcher_paused", "false")

    def is_paused(self) -> bool:
        return self._paused

    async def status(self) -> dict:
        entries = await self.list_watched()
        return {
            "paused": self._paused,
            "watched": [e.canonical_symbol for e in entries],
            "cycles": self.cycles,
            "events_considered": self.events_considered,
            "alerts_published": self.alerts_published,
        }

    # ---- provider/gate symbol admission ------------------------------------

    def _admit_symbol(self, symbol: str) -> None:
        """Start collecting real structured data for `symbol` and make its
        raw events visible as deterministic NOTIFY alerts -- never
        analyze-eligible on their own (mission: only this service's own
        correlated synthesis reaches Claude for a newly-watched symbol)."""
        sym = symbol.upper()
        self._gate.allow_symbol(sym, analyze=False)
        for provider_list in self._provider_symbol_lists():
            if sym not in provider_list:
                provider_list.append(sym)
                self._added_provider_symbols.add(sym)
        self._price_history.setdefault(sym, deque())
        self._evidence.setdefault(sym, deque())

    def _dismiss_symbol(self, symbol: str) -> None:
        sym = symbol.upper()
        self._gate.disallow_symbol(sym)
        if sym in self._added_provider_symbols:
            for provider_list in self._provider_symbol_lists():
                if sym in provider_list:
                    provider_list.remove(sym)
            self._added_provider_symbols.discard(sym)
        self._price_history.pop(sym, None)
        self._evidence.pop(sym, None)

    def _provider_symbol_lists(self) -> list[list[str]]:
        lists = []
        for attr in ("mt5", "calendar", "news"):
            provider = getattr(self._monitors, attr, None)
            symbols = getattr(provider, "symbols", None)
            if isinstance(symbols, list):
                lists.append(symbols)
        return lists

    # ---- periodic tick: price-history sampling (volatility-relative) ------

    async def tick(self) -> None:
        self.cycles += 1
        mt5 = getattr(self._monitors, "mt5", None)
        if mt5 is None:
            return
        try:
            quotes = mt5.snapshot().get("quotes") or {}
        except Exception:
            return
        now = self._clock()
        for symbol, quote in quotes.items():
            if symbol not in self._price_history:
                continue
            bid, ask = quote.get("bid"), quote.get("ask")
            if bid is None or ask is None:
                continue
            mid = (bid + ask) / 2
            self._price_history[symbol].append((now, mid))
            cutoff = now - timedelta(seconds=self.correlation_window_seconds)
            hist = self._price_history[symbol]
            while hist and hist[0][0] < cutoff:
                hist.popleft()

    # ---- event subscription: calendar/news/price correlation --------------

    async def _on_event(self, payload: dict) -> None:
        if self._paused:
            return
        if payload.get("type") != "proactive_event":
            return
        event = payload.get("event") or {}
        symbol = event.get("symbol")
        if not symbol or symbol.upper() not in self._evidence:
            return  # not a watched symbol
        symbol = symbol.upper()
        source, kind = event.get("source"), event.get("kind")
        now = self._clock()

        if source == EventSource.MT5.value and kind in _PRICE_KINDS:
            self._record_evidence(symbol, "price", now, event)
        elif source == EventSource.ECONOMIC_CALENDAR.value and kind in _CALENDAR_KINDS:
            self._record_evidence(symbol, "calendar", now, event)
        elif source == EventSource.NEWS.value and kind == "news.headline":
            self._record_evidence(symbol, "news", now, event)
        else:
            return

        self.events_considered += 1
        await self._evaluate(symbol, now)

    def _record_evidence(self, symbol: str, kind: str, at: datetime, event: dict) -> None:
        buf = self._evidence.setdefault(symbol, deque())
        buf.append(_EvidenceItem(kind=kind, at=at, detail=event, evidence_id=event.get("dedupe_key") or event.get("id", "")))
        cutoff = at - timedelta(seconds=self.correlation_window_seconds)
        while buf and buf[0].at < cutoff:
            buf.popleft()

    # ---- correlation + significance + escalation + publish ----------------

    async def _evaluate(self, symbol: str, now: datetime) -> None:
        candidate = self._build_candidate(symbol, now)
        if candidate.factor_count < 2 and not candidate.move_is_unusual_without_obvious_cause:
            # A single ordinary factor (one calendar reminder, one headline,
            # one plain price move) is already visible as its own raw
            # per-source NOTIFY event (mission section 11: "CPI in 15
            # minutes" alone needs no Claude AND no second alert) --
            # publishing a SYSTEM-level synthesis on top would duplicate
            # it, violating "Notify once." Only a genuine multi-factor
            # correlation or an unusual-without-cause move earns one.
            return

        signature = candidate.evidence_ids
        last = self._last_published.get(symbol)
        if last is not None:
            last_at, last_signature = last
            same_story = signature == last_signature
            within_cooldown = (now - last_at).total_seconds() < self.cooldown_seconds
            if same_story and within_cooldown:
                return  # mission section 10: no repeat spam for the same development

        escalate = requires_reasoning(candidate)
        causality = _causality_class(candidate)
        severity = 3 if escalate else 2
        title, summary = self._describe(candidate, causality)

        published = await self._core.publish(NormalizedEvent(
            source=EventSource.SYSTEM, kind="watcher.correlated_alert", severity=severity, symbol=symbol,
            title=title[:200], summary=summary[:2000],
            dedupe_key=f"watch:{symbol}:{int(now.timestamp() // self.cooldown_seconds)}",
            expires_at=now + timedelta(minutes=30),
            payload={
                "factor_count": candidate.factor_count, "causality_class": causality,
                "move_pct": candidate.move_pct, "has_calendar_event": candidate.has_calendar_event,
                "calendar_title": candidate.calendar_title, "has_relevant_news": candidate.has_relevant_news,
                "news_headlines": list(candidate.news_headlines), "has_open_position": candidate.has_open_position,
                "requires_reasoning": escalate, "evidence_ids": list(signature),
            },
        ))
        if published:
            self._last_published[symbol] = (now, signature)
            self.alerts_published += 1
            logger.info("[market_watch] correlated alert for %s (factors=%d, escalate=%s)",
                       symbol, candidate.factor_count, escalate)

    def _build_candidate(self, symbol: str, now: datetime) -> EventCandidate:
        buf = self._evidence.get(symbol) or deque()
        price_items = [e for e in buf if e.kind == "price"]
        calendar_items = [e for e in buf if e.kind == "calendar"]
        news_items = [e for e in buf if e.kind == "news"]

        candidate = EventCandidate(symbol=symbol)
        evidence_ids: list[str] = []

        if price_items:
            latest = price_items[-1]
            candidate.has_price_move = True
            candidate.move_pct = (latest.detail.get("payload") or {}).get("move_pct")
            evidence_ids.append(latest.evidence_id)
        significance = self._price_significance(symbol, now)
        if significance is not None:
            is_unusual, insufficient = significance
            candidate.insufficient_price_history = insufficient
            if candidate.has_price_move and is_unusual and not (calendar_items or news_items):
                candidate.move_is_unusual_without_obvious_cause = True

        if calendar_items:
            latest = calendar_items[-1]
            candidate.has_calendar_event = True
            candidate.calendar_title = latest.detail.get("title", "")
            candidate.calendar_event_id = (latest.detail.get("payload") or {}).get("event")
            evidence_ids.append(latest.evidence_id)

        if news_items:
            candidate.has_relevant_news = True
            candidate.news_headlines = tuple(n.detail.get("title", "") for n in news_items[-3:])
            evidence_ids.extend(n.evidence_id for n in news_items[-3:])

        candidate.has_open_position = self._has_open_position(symbol)
        candidate.evidence_ids = tuple(sorted(set(evidence_ids)))
        return candidate

    def _price_significance(self, symbol: str, now: datetime) -> tuple[bool, bool] | None:
        """Volatility-relative significance (mission section 5): is the
        MOST RECENT sample-to-sample change large relative to this
        symbol's own recent average absolute change? Returns
        (is_unusual, insufficient_history); None if there is no price
        history at all yet for this symbol."""
        hist = self._price_history.get(symbol)
        if not hist or len(hist) < 2:
            return None
        prices = [p for _, p in hist]
        changes_pct = [
            abs(prices[i] - prices[i - 1]) / prices[i - 1] * 100
            for i in range(1, len(prices)) if prices[i - 1]
        ]
        if not changes_pct:
            return None
        latest_change = changes_pct[-1]
        if len(changes_pct) < self.min_price_samples:
            # Not enough history for a defensible volatility baseline --
            # conservative fixed fallback, explicitly marked as such.
            return latest_change >= self.fallback_abs_move_pct, True
        baseline = sum(changes_pct[:-1]) / len(changes_pct[:-1])
        if baseline <= 0:
            return latest_change >= self.fallback_abs_move_pct, False
        return latest_change >= baseline * self.volatility_multiplier, False

    def _has_open_position(self, symbol: str) -> bool:
        mt5 = getattr(self._monitors, "mt5", None)
        if mt5 is None:
            return False
        try:
            positions = mt5.snapshot().get("positions") or []
        except Exception:
            return False
        return any(p.get("symbol") == symbol for p in positions)

    @staticmethod
    def _describe(candidate: EventCandidate, causality: str) -> tuple[str, str]:
        parts = []
        if candidate.has_price_move and candidate.move_pct is not None:
            direction = "up" if candidate.move_pct > 0 else "down"
            parts.append(f"moved {direction} {abs(candidate.move_pct):.2f}%")
        if candidate.has_calendar_event:
            parts.append(f"calendar: {candidate.calendar_title}" if candidate.calendar_title else "a calendar event")
        if candidate.has_relevant_news and candidate.news_headlines:
            parts.append(f"news: {candidate.news_headlines[-1]}")
        title = f"{candidate.symbol}: " + "; ".join(parts) if parts else f"{candidate.symbol}: notable development"
        hedge = {
            "STRONG_TEMPORAL": "the timing is consistent with",
            "PLAUSIBLE": "this may be related to",
            "INSUFFICIENT": "no clear single catalyst identified for",
        }[causality]
        summary = f"{title}. Evidence class: {causality} -- {hedge} the factors observed."
        return title, summary
