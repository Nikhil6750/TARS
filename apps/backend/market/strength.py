"""Eight-currency relative strength from real pair returns.

Method, stated plainly so the numbers can be checked by hand:

1. For each available pair, take the volatility-normalised return (`z`) over
   the requested window.
2. Give it the sign of the currency's role: ``+z`` where the currency is the
   base, ``-z`` where it is the quote. A rise in EURUSD is EUR up and USD
   down by the same amount.
3. A currency's score is the mean of those signed contributions.

Because the pair set on this machine is uneven -- NZD appears in one pair and
CHF in two -- every score carries its own coverage record and a
leave-one-pair-out diagnostic. A score derived from one pair is reported as
exactly that, never presented as equal in standing to a score built from six.

This is a measure of relative price movement. It is not a flow measurement,
not a forecast, and carries no probability.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from market import universe
from market.indicators import normalized_return

# Windows offered to the UI, expressed in bars of the timeframe they are
# computed on. The timeframe is chosen so each window spans roughly its name.
WINDOWS: dict[str, tuple[str, int]] = {
    "1H": ("M5", 12),     # 12 x M5  = 1 hour
    "4H": ("M15", 16),    # 16 x M15 = 4 hours
    "1D": ("H1", 24),     # 24 x H1  = 1 day
}
DEFAULT_WINDOW = "1D"

# Below this many pairs a currency's score is flagged as thinly covered.
THIN_COVERAGE_PAIRS = 3


@dataclass
class Contribution:
    pair: str
    role: str          # BASE | QUOTE
    sign: int
    pair_z: float
    contribution: float
    pct_change: float

    def as_dict(self) -> dict:
        return {
            "pair": self.pair,
            "role": self.role,
            "sign": self.sign,
            "pair_z": round(self.pair_z, 4),
            "contribution": round(self.contribution, 4),
            "pct_change": round(self.pct_change, 4),
        }


@dataclass
class CurrencyStrength:
    currency: str
    score: float
    direction: str                 # BULLISH | BEARISH | NEUTRAL
    pairs_used: int
    pairs_possible: int
    counterparties: list[str]
    coverage_ratio: float
    thin_coverage: bool
    # Largest absolute change in `score` when any single pair is removed.
    # A large value means one pair is carrying the reading.
    leave_one_out_max_delta: float
    leave_one_out_driver: str | None
    contributions: list[Contribution] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "currency": self.currency,
            "score": round(self.score, 4),
            "direction": self.direction,
            "pairs_used": self.pairs_used,
            "pairs_possible": self.pairs_possible,
            "counterparties": self.counterparties,
            "coverage_ratio": round(self.coverage_ratio, 4),
            "thin_coverage": self.thin_coverage,
            "leave_one_out_max_delta": round(self.leave_one_out_max_delta, 4),
            "leave_one_out_driver": self.leave_one_out_driver,
            "contributions": [c.as_dict() for c in self.contributions],
        }


@dataclass
class StrengthSnapshot:
    window: str
    timeframe: str
    bars: int
    as_of: str
    ranking: list[CurrencyStrength]
    pairs_evaluated: list[str]
    pairs_missing: list[str]
    method: str

    def as_dict(self) -> dict:
        return {
            "window": self.window,
            "timeframe": self.timeframe,
            "bars": self.bars,
            "as_of": self.as_of,
            "ranking": [r.as_dict() for r in self.ranking],
            "pairs_evaluated": self.pairs_evaluated,
            "pairs_missing": self.pairs_missing,
            "method": self.method,
        }


_METHOD = (
    "Mean of volatility-normalised pair returns, signed by the currency's role "
    "(+ as base, - as quote). Relative price movement only; not a flow "
    "measurement and not a forecast."
)


def _direction(score: float) -> str:
    if score > 0.15:
        return "BULLISH"
    if score < -0.15:
        return "BEARISH"
    return "NEUTRAL"


def compute_strength(store, *, as_of, window: str = DEFAULT_WINDOW) -> StrengthSnapshot:
    """Rank the eight currencies as of `as_of` using only completed bars."""
    if window not in WINDOWS:
        raise ValueError("unknown strength window " + window)
    timeframe, bars = WINDOWS[window]

    available = set(store.available_symbols)
    pair_z: dict[str, tuple[float, float]] = {}
    missing: list[str] = []

    for pair in universe.FX_PAIRS:
        if pair not in available:
            missing.append(pair)
            continue
        closes = [c for _, c in store.close_series(
            pair, timeframe, as_of=as_of, limit=bars + 120
        )]
        nr = normalized_return(closes, window=bars)
        if nr is None:
            missing.append(pair)
            continue
        pair_z[pair] = (nr.z, nr.pct_change)

    ranking: list[CurrencyStrength] = []
    for currency in universe.CURRENCIES:
        contributions: list[Contribution] = []
        for pair in universe.pairs_for_currency(currency):
            if pair not in pair_z:
                continue
            z, pct = pair_z[pair]
            meta = universe.INSTRUMENTS[pair]
            is_base = meta.base == currency
            sign = 1 if is_base else -1
            contributions.append(
                Contribution(
                    pair=pair,
                    role="BASE" if is_base else "QUOTE",
                    sign=sign,
                    pair_z=z,
                    contribution=sign * z,
                    pct_change=pct if is_base else -pct,
                )
            )

        used = len(contributions)
        score = sum(c.contribution for c in contributions) / used if used else 0.0

        # Leave-one-pair-out: how much does the score move if any single pair
        # is dropped? Reported, not corrected for.
        loo_delta, loo_driver = 0.0, None
        if used > 1:
            for c in contributions:
                rest = [o for o in contributions if o.pair != c.pair]
                alt = sum(o.contribution for o in rest) / len(rest)
                if abs(alt - score) > loo_delta:
                    loo_delta, loo_driver = abs(alt - score), c.pair
        elif used == 1:
            # One pair cannot be cross-checked at all; say so explicitly.
            loo_delta, loo_driver = abs(score), contributions[0].pair

        counterparties = sorted(
            {
                universe.INSTRUMENTS[c.pair].quote
                if universe.INSTRUMENTS[c.pair].base == currency
                else universe.INSTRUMENTS[c.pair].base
                for c in contributions
            }
        )
        possible = len(universe.pairs_for_currency(currency))
        ranking.append(
            CurrencyStrength(
                currency=currency,
                score=score,
                direction=_direction(score) if used else "NEUTRAL",
                pairs_used=used,
                pairs_possible=possible,
                counterparties=counterparties,
                coverage_ratio=(used / possible) if possible else 0.0,
                thin_coverage=used < THIN_COVERAGE_PAIRS,
                leave_one_out_max_delta=loo_delta,
                leave_one_out_driver=loo_driver,
                contributions=sorted(contributions, key=lambda c: -abs(c.contribution)),
            )
        )

    ranking.sort(key=lambda r: -r.score)
    return StrengthSnapshot(
        window=window,
        timeframe=timeframe,
        bars=bars,
        as_of=as_of.isoformat(),
        ranking=ranking,
        pairs_evaluated=sorted(pair_z),
        pairs_missing=sorted(set(missing)),
        method=_METHOD,
    )
