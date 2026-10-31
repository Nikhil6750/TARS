"""Where a displayed observation came from (task P2-C3).

Every payload the market service returns carries one of these, so a client
never has to infer the source of what it is drawing. Two sources exist and
they are not interchangeable:

``RECORDED_STORE``
    Candles read from the Parquet store on this machine. Recorded history.
    Honest, deterministic, and **not** a live market.

``LIVE_FEED``
    Ticks from an authenticated provider, within the freshness window.

The distinction already governed the feed status; carrying it on the data
itself is what makes it checkable from outside the backend. A client, a test
or a reviewer can read ``provenance.source`` and know, rather than trusting
that the status label was derived correctly somewhere upstream.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class DataSource(str, Enum):
    RECORDED_STORE = "RECORDED_STORE"
    LIVE_FEED = "LIVE_FEED"


# The provider name used when nothing is authenticated. Deliberately explicit
# rather than None, so a payload says what it is instead of omitting it.
NO_PROVIDER = "none"
STORE_PROVIDER = "parquet_store"


@dataclass(frozen=True)
class Provenance:
    """The source of the observations in the payload that carries this."""

    source: DataSource
    provider: str
    # The moment the data itself describes -- the cursor under replay, the
    # last tick under a live feed. Not the time the request was served.
    observed_at: datetime | None
    replay: bool
    detail: str
    # Whether the feed behind this is fresh right now. Data from a feed that
    # has gone stale or shut for the weekend still came from that feed, so
    # `source` stays LIVE_FEED, but it is not live.
    fresh: bool = False

    @property
    def is_live(self) -> bool:
        return self.source is DataSource.LIVE_FEED and not self.replay and self.fresh

    def as_dict(self) -> dict:
        return {
            "source": self.source.value,
            "provider": self.provider,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "replay": self.replay,
            "detail": self.detail,
            "is_live": self.is_live,
        }


def recorded(*, observed_at: datetime | None, detail: str = "") -> Provenance:
    """Provenance for anything computed from the candle store."""
    return Provenance(
        source=DataSource.RECORDED_STORE,
        provider=STORE_PROVIDER,
        observed_at=observed_at,
        replay=True,
        detail=detail or "Computed from recorded candles on this machine. Not a live feed.",
    )


def live(*, provider: str, observed_at: datetime | None, fresh: bool, detail: str = "") -> Provenance:
    """Provenance for anything computed from an authenticated feed's bars."""
    return Provenance(
        source=DataSource.LIVE_FEED,
        provider=provider,
        observed_at=observed_at,
        replay=False,
        fresh=fresh,
        detail=detail or (
            ("Live observations from " if fresh else "Last observations from ") + provider + "."
        ),
    )
