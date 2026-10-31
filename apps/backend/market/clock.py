"""Market sessions, feed status, and the replay cursor.

The single most important rule in this module: an observation is only ever
labelled ``LIVE`` when a real feed produced it within the freshness window
*and* the market is open. Recorded history replayed through the dashboard is
``REPLAY``; a feed that has fallen behind is ``STALE``; outside trading hours
it is ``CLOSED``. Nothing here can produce a ``LIVE`` label from stored
candles, because stored candles never pass through `live_status`.

`resolve_status` is the single gate every caller must use, and the only
function that calls `live_status`. A fresh tick is not enough to say LIVE: the
label describes the panels, so it also requires that they are being computed
from the feed's own bars. That rules out a pinned replay cursor, and equally a
feed that has connected but has not delivered bars yet, when everything on
screen would still be recorded history.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from enum import Enum

from market.provenance import DataSource

# FX trades continuously from Sunday 21:00 UTC to Friday 21:00 UTC (approximating
# the 17:00 New York convention; it shifts by an hour across DST changes and is
# reported as an approximation rather than an exchange calendar).
_WEEK_OPEN = (6, time(21, 0))   # Sunday 21:00 UTC
_WEEK_CLOSE = (4, time(21, 0))  # Friday 21:00 UTC

# How old the newest real tick may be before the feed is called STALE.
LIVE_FRESHNESS_SECONDS = 90.0


class FeedStatus(str, Enum):
    LIVE = "LIVE"
    STALE = "STALE"
    REPLAY = "REPLAY"
    CLOSED = "CLOSED"
    NO_DATA = "NO_DATA"


class SessionName(str, Enum):
    SYDNEY = "SYDNEY"
    TOKYO = "TOKYO"
    LONDON = "LONDON"
    NEW_YORK = "NEW_YORK"
    OFF_HOURS = "OFF_HOURS"


# Approximate liquidity sessions in UTC. Labelled approximate because they do
# not shift with local daylight saving.
_SESSIONS: tuple[tuple[SessionName, int, int], ...] = (
    (SessionName.SYDNEY, 21, 6),
    (SessionName.TOKYO, 0, 9),
    (SessionName.LONDON, 7, 16),
    (SessionName.NEW_YORK, 12, 21),
)


@dataclass(frozen=True)
class MarketStatus:
    status: FeedStatus
    market_open: bool
    as_of: datetime
    # The newest real observation the dashboard is working from.
    data_as_of: datetime | None
    data_age_seconds: float | None
    sessions_active: tuple[str, ...]
    next_transition: datetime | None
    detail: str
    replay: bool
    # Which kind of source produced `data_as_of`. Defaulted to the recorded
    # store because that is the safe answer: a status that forgets to say
    # it is live is read as recorded, never the other way round.
    source: str = DataSource.RECORDED_STORE.value
    # The state of the connection itself, which is a different thing from the
    # status: an unconfigured feed and a dropped one both show recorded data,
    # and only one of them is something to fix.
    feed_connection: str = "NOT_CONFIGURED"
    feed_detail: str = ""

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "market_open": self.market_open,
            "as_of": self.as_of.isoformat(),
            "data_as_of": self.data_as_of.isoformat() if self.data_as_of else None,
            "data_age_seconds": self.data_age_seconds,
            "sessions_active": list(self.sessions_active),
            "next_transition": self.next_transition.isoformat() if self.next_transition else None,
            "detail": self.detail,
            "replay": self.replay,
            "source": self.source,
            "feed_connection": self.feed_connection,
            "feed_detail": self.feed_detail,
        }


def _at(moment: datetime, weekday: int, clock: time) -> datetime:
    """The given weekday/time in the ISO week containing `moment`."""
    base = moment.astimezone(UTC)
    delta = weekday - base.weekday()
    return (base + timedelta(days=delta)).replace(
        hour=clock.hour, minute=clock.minute, second=0, microsecond=0
    )


def is_market_open(moment: datetime) -> bool:
    m = moment.astimezone(UTC)
    close = _at(m, _WEEK_CLOSE[0], _WEEK_CLOSE[1])
    open_ = _at(m, _WEEK_OPEN[0], _WEEK_OPEN[1])
    if m.weekday() == 5:                      # Saturday: always shut
        return False
    if m.weekday() == 6:                      # Sunday: opens in the evening
        return m >= open_
    if m.weekday() == 4:                      # Friday: shuts in the evening
        return m < close
    return True


def next_transition(moment: datetime) -> datetime:
    """When the market next opens or closes after `moment`."""
    m = moment.astimezone(UTC)
    if is_market_open(m):
        friday_close = _at(m, _WEEK_CLOSE[0], _WEEK_CLOSE[1])
        return friday_close if m < friday_close else friday_close + timedelta(days=7)
    sunday_open = _at(m, _WEEK_OPEN[0], _WEEK_OPEN[1])
    return sunday_open if m < sunday_open else sunday_open + timedelta(days=7)


def active_sessions(moment: datetime) -> tuple[str, ...]:
    if not is_market_open(moment):
        return ()
    hour = moment.astimezone(UTC).hour
    names = [
        name.value
        for name, start, end in _SESSIONS
        if (start <= hour < end if start < end else hour >= start or hour < end)
    ]
    return tuple(names) or (SessionName.OFF_HOURS.value,)


def live_status(
    *,
    now: datetime,
    feed_last_tick: datetime | None,
    freshness_seconds: float = LIVE_FRESHNESS_SECONDS,
    connected: bool = True,
) -> MarketStatus:
    """Status of a *real* feed. Only this function can return LIVE."""
    open_ = is_market_open(now)
    age = (now - feed_last_tick).total_seconds() if feed_last_tick else None

    if feed_last_tick is None:
        status = FeedStatus.CLOSED if not open_ else FeedStatus.NO_DATA
        detail = (
            "Market closed; no live feed connected."
            if not open_
            else "Market open but no live feed is connected."
        )
    elif not open_:
        status = FeedStatus.CLOSED
        detail = "Market closed. Showing the last recorded observations."
    elif not connected:
        status = FeedStatus.STALE
        detail = "Live feed disconnected; last tick was " + _humanize(age or 0.0) + " ago."
    elif age is not None and age <= freshness_seconds:
        status = FeedStatus.LIVE
        detail = "Live feed healthy."
    else:
        status = FeedStatus.STALE
        detail = "Live feed connected but last tick is " + _humanize(age or 0.0) + " old."

    return MarketStatus(
        status=status,
        market_open=open_,
        as_of=now,
        data_as_of=feed_last_tick,
        data_age_seconds=age,
        sessions_active=active_sessions(now),
        next_transition=next_transition(now),
        detail=detail,
        replay=False,
        source=DataSource.LIVE_FEED.value,
    )


def replay_status(*, now: datetime, cursor: datetime, store_last_bar: datetime | None) -> MarketStatus:
    """Status while the dashboard is driven by recorded history. Never LIVE."""
    age = (now - cursor).total_seconds()
    detail = (
        "Replaying recorded history as of "
        + cursor.strftime("%Y-%m-%d %H:%M")
        + " UTC ("
        + _humanize(age)
        + " behind now). Not a live feed."
    )
    if store_last_bar is not None and cursor >= store_last_bar:
        detail = (
            "Showing the newest recorded bar ("
            + cursor.strftime("%Y-%m-%d %H:%M")
            + " UTC, " + _humanize(age) + " old). Recorded history, not a live feed."
        )
    return MarketStatus(
        status=FeedStatus.REPLAY,
        market_open=is_market_open(now),
        as_of=now,
        data_as_of=cursor,
        data_age_seconds=age,
        sessions_active=active_sessions(cursor),
        next_transition=next_transition(now),
        detail=detail,
        replay=True,
        source=DataSource.RECORDED_STORE.value,
    )


def no_data_status(*, now: datetime, detail: str) -> MarketStatus:
    """Nothing to show at all -- an empty store, typically."""
    return MarketStatus(
        status=FeedStatus.NO_DATA,
        market_open=is_market_open(now),
        as_of=now,
        data_as_of=None,
        data_age_seconds=None,
        sessions_active=active_sessions(now),
        next_transition=next_transition(now),
        detail=detail,
        replay=False,
        source=DataSource.RECORDED_STORE.value,
    )


def resolve_status(
    *,
    now: datetime,
    feed_last_tick: datetime | None,
    replay_pinned: bool,
    panels_follow_feed: bool,
    cursor: datetime | None,
    store_last_bar: datetime | None,
    freshness_seconds: float = LIVE_FRESHNESS_SECONDS,
    no_data_detail: str = "No market history yet. Import it from the dashboard.",
    feed_connection: str = "NOT_CONFIGURED",
    feed_detail: str = "",
) -> MarketStatus:
    """The one gate that decides a feed status.

    Precedence, and the reason for it:

    1. **A pinned replay cursor wins over everything.** The panels were
       computed at ``ts <= cursor``; a fresh tick elsewhere does not make
       rewound data live.
    2. Panels not computed from the feed's bars -> recorded history (or
       NO_DATA with no store), however healthy the feed itself is.
    3. Only then may a real feed be judged LIVE or STALE.

    LIVE is unreachable through this function unless `panels_follow_feed` is
    true and `replay_pinned` is false, whatever the ticks are doing.
    """
    connected = feed_connection == "CONNECTED"
    if replay_pinned or not panels_follow_feed or feed_last_tick is None:
        if cursor is None:
            status = no_data_status(now=now, detail=no_data_detail)
        else:
            status = replay_status(now=now, cursor=cursor, store_last_bar=store_last_bar)
    else:
        status = live_status(
            now=now, feed_last_tick=feed_last_tick, freshness_seconds=freshness_seconds,
            connected=connected,
        )
    return replace(status, feed_connection=feed_connection, feed_detail=feed_detail)


def _humanize(seconds: float) -> str:
    seconds = abs(seconds)
    if seconds < 90:
        return str(int(seconds)) + "s"
    if seconds < 5400:
        return str(int(seconds // 60)) + "m"
    if seconds < 172800:
        return str(int(seconds // 3600)) + "h"
    return str(int(seconds // 86400)) + "d"
