"""The instrument universe actually backed by data on this machine.

Deliberately *not* a wish list: every entry here was verified against a real
file on disk during ingestion (see `market.ingest`). Instruments whose data
is absent are absent from this module, so the dashboard can never show a
symbol it has no observations for.
"""
from __future__ import annotations

from dataclasses import dataclass

# The eight majors the currency-strength model ranks.
CURRENCIES: tuple[str, ...] = ("USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF")


@dataclass(frozen=True)
class Instrument:
    symbol: str
    asset_class: str          # FX | METAL
    base: str
    quote: str
    price_precision: int
    pip_size: float
    # Sub-directory under the CSV root plus the filename stem pattern, used
    # only by the ingester.
    source_dir: str
    source_stem: str

    @property
    def is_fx_pair(self) -> bool:
        """True when both legs are ranked currencies, i.e. this pair carries
        information for the currency-strength model."""
        return self.asset_class == "FX" and self.base in CURRENCIES and self.quote in CURRENCIES


_FX_PAIRS = (
    "AUDCAD", "AUDCHF", "AUDJPY", "AUDUSD", "CHFJPY", "EURAUD", "EURCAD",
    "EURGBP", "EURJPY", "EURUSD", "GBPAUD", "GBPCAD", "GBPJPY", "GBPUSD",
    "NZDUSD", "USDCAD", "USDJPY",
)

# JPY crosses quote to 3 decimals and use a 0.01 pip; everything else is 5/0.0001.
_JPY_QUOTED = {"JPY"}


def _fx(symbol: str) -> Instrument:
    base, quote = symbol[:3], symbol[3:]
    jpy = quote in _JPY_QUOTED
    return Instrument(
        symbol=symbol, asset_class="FX", base=base, quote=quote,
        price_precision=3 if jpy else 5,
        pip_size=0.01 if jpy else 0.0001,
        source_dir="Forex_UTC",
        source_stem=f"{symbol}_1 Min",
    )


INSTRUMENTS: dict[str, Instrument] = {s: _fx(s) for s in _FX_PAIRS}
INSTRUMENTS["XAUUSD"] = Instrument(
    symbol="XAUUSD", asset_class="METAL", base="XAU", quote="USD",
    price_precision=3, pip_size=0.1,
    source_dir="xauusd_utc", source_stem="XAUUSD_1 Min",
)

SYMBOLS: tuple[str, ...] = tuple(INSTRUMENTS)
FX_PAIRS: tuple[str, ...] = tuple(s for s, m in INSTRUMENTS.items() if m.is_fx_pair)

# Timeframes the dashboard can display, with their bar length in minutes.
TIMEFRAMES: dict[str, int] = {"M5": 5, "M15": 15, "H1": 60, "H4": 240, "D1": 1440}
DEFAULT_TIMEFRAME = "H1"


def get(symbol: str) -> Instrument:
    key = symbol.upper().replace("/", "")
    if key not in INSTRUMENTS:
        raise KeyError(f"{symbol!r} is not in the data-backed instrument universe")
    return INSTRUMENTS[key]


def pairs_for_currency(currency: str) -> tuple[str, ...]:
    return tuple(s for s in FX_PAIRS if currency in (INSTRUMENTS[s].base, INSTRUMENTS[s].quote))
