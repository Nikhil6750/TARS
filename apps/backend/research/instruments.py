"""Provider-independent instrument metadata. No global pip-size assumption: every instrument carries its own
precision / tick / pip / contract / session model / closure rules / cost defaults (all explicit, all assumptions labelled)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time


@dataclass(frozen=True)
class CostDefaults:
    """Research cost assumptions in PRICE units. Spread comes from real bid/ask; these are the *additional* explicit
    assumptions, each separate, each reported."""
    commission_round_turn: float
    slippage_per_side: float
    latency_bars: int
    financing_per_bar: float = 0.0
    financing_enabled: bool = False
    notes: str = ""


@dataclass(frozen=True)
class InstrumentMeta:
    instrument: str
    asset_class: str                 # FX | METAL | ...
    price_precision: int             # decimals quoted by the source
    tick_size: float
    pip_size: float | None           # None where "pip" is not a meaningful unit
    contract_size: float | None      # units per 1.00 lot
    contract_unit: str               # e.g. "troy oz", "base currency"
    quote_currency: str
    timezone: str                    # IANA tz of the instrument's trading-day convention
    week_close_local: tuple[int, time]   # (weekday 0=Mon, local time) in `timezone`
    week_open_local: tuple[int, time]
    daily_break_local: tuple[time, time] | None  # recurring daily maintenance break in `timezone`
    daily_anchor_local: time          # D1 bar anchor in `timezone`
    typical_trading_days: str
    costs: CostDefaults
    aliases: tuple[str, ...] = field(default_factory=tuple)


_NY = "America/New_York"

INSTRUMENTS: dict[str, InstrumentMeta] = {
    "XAUUSD": InstrumentMeta(
        "XAUUSD", "METAL", 3, 0.001, 0.1, 100.0, "troy oz", "USD", _NY,
        (4, time(17, 0)), (6, time(18, 0)), (time(17, 0), time(18, 0)), time(17, 0),
        "Sun 18:00 - Fri 17:00 New York, with a 17:00-18:00 daily break",
        CostDefaults(commission_round_turn=0.07, slippage_per_side=0.02, latency_bars=0,
                     notes="ASSUMPTIONS: commission $7/lot round turn = $0.07/oz (100 oz lot); 2 cents/oz adverse slippage per side. "
                           "Spread is taken from real bid/ask. Financing disabled (intraday research placeholder)."),
        ("GOLD", "XAU/USD")),
    "EURUSD": InstrumentMeta(
        "EURUSD", "FX", 5, 0.00001, 0.0001, 100000.0, "EUR", "USD", _NY,
        (4, time(17, 0)), (6, time(17, 0)), None, time(17, 0),
        "Sun 17:00 - Fri 17:00 New York",
        CostDefaults(commission_round_turn=0.00007, slippage_per_side=0.00001, latency_bars=0,
                     notes="ASSUMPTIONS: commission $7/lot round turn = 0.7 pip; 0.1 pip slippage per side. Financing disabled."),
        ("EUR/USD",)),
}


def get_meta(instrument: str) -> InstrumentMeta:
    key = instrument.upper().replace("/", "")
    if key in INSTRUMENTS:
        return INSTRUMENTS[key]
    for m in INSTRUMENTS.values():
        if key in {a.upper().replace("/", "") for a in m.aliases}:
            return m
    raise KeyError(f"no instrument metadata for {instrument}; add it to research.instruments.INSTRUMENTS")


def has_meta(instrument: str) -> bool:
    try:
        get_meta(instrument)
        return True
    except KeyError:
        return False


def pip_size_of(instrument: str) -> float:
    """Pip size from metadata; JPY-quoted pairs fall back to 0.01, other unknown pairs to 0.0001 (labelled legacy default)."""
    if has_meta(instrument):
        m = get_meta(instrument)
        if m.pip_size is None:
            raise ValueError(f"{instrument} has no pip concept; use tick_size")
        return m.pip_size
    return 0.01 if instrument.upper().endswith("JPY") else 0.0001
