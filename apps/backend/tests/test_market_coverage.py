"""Coverage warnings and data backing survive an expanded universe (P2-C5).

The `thin_coverage` flag and the leave-one-pair-out diagnostic are what make
uneven coverage visible: NZD rests on one pair here and CHF on two, and a
score built from one pair is reported as exactly that. Expanding coverage
towards the 26-instrument target (P2-C4) is the change most likely to
weaken that machinery -- silently closing the gap would be worse than the
gap, because a reader would no longer be able to tell which scores are
thin.

These tests use a stub store. The subject is the coverage bookkeeping over
a given set of pair returns, not the Parquet reader, which
`test_market_xray.py` covers against real candles.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from market import strength, universe
from market.universe import DataBacking

AS_OF = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)


class StubStore:
    """Returns a close series engineered to give each pair a known move."""

    def __init__(self, symbols, drift_per_pair=None):
        self._symbols = list(symbols)
        self._drift = drift_per_pair or {}

    @property
    def available_symbols(self):
        return list(self._symbols)

    # The alternating wobble sets the volatility the return is normalised
    # against. It has to be comparable to the drift, or every pair saturates
    # the +/-6 clamp in `normalized_return` and all of them look identical --
    # which is exactly what made an earlier version of these tests unable to
    # tell a dominating pair from an ordinary one.
    NOISE = 0.002

    def close_series(self, symbol, timeframe, *, as_of, limit):
        drift = self._drift.get(symbol, 0.0004)
        out = []
        price = 100.0
        for i in range(limit):
            price *= 1.0 + drift + (self.NOISE if i % 2 else -self.NOISE)
            out.append((i, price))
        return out


# ---- data backing is mandatory -----------------------------------------


def test_every_instrument_declares_its_data_backing():
    for symbol, meta in universe.INSTRUMENTS.items():
        assert meta.backing, f"{symbol} does not declare its data backing"
        assert meta.backing in {b.value for b in DataBacking}, f"{symbol}: {meta.backing}"


def test_an_instrument_cannot_be_created_without_declaring_its_backing():
    """Structural, not conventional: the field has no default, so a new
    instrument added without a backing fails at construction rather than
    reaching the dashboard undeclared."""
    with pytest.raises(TypeError):
        universe.Instrument(  # type: ignore[call-arg]
            symbol="XXXYYY", asset_class="FX", base="XXX", quote="YYY",
            price_precision=5, pip_size=0.0001,
            source_dir="d", source_stem="s",
        )


def test_everything_shipped_today_is_recorded_history():
    """Nothing here came from a live feed, so nothing may claim to have."""
    for symbol, meta in universe.INSTRUMENTS.items():
        assert meta.backing == DataBacking.RECORDED_CSV.value, symbol


def test_the_service_reports_each_instrument_s_backing():
    from market.service import MarketXRayService
    from market.store import CandleStore

    store = CandleStore()
    if not store.available_symbols:
        pytest.skip("candle store is empty; run `python -m market.ingest`")
    svc = MarketXRayService(store=store)
    rows = svc.instruments()
    assert rows
    for row in rows:
        assert row["backing"] in {b.value for b in DataBacking}
        # The declaration and the evidence must agree: a recorded instrument
        # has to actually have bars behind it.
        assert row["bars_m5"] > 0, row["symbol"]


# ---- the unavailable pairs stay unavailable ----------------------------


def test_unavailable_pairs_are_declared_and_really_absent():
    assert universe.UNAVAILABLE_PAIRS
    for pair in universe.UNAVAILABLE_PAIRS:
        assert pair not in universe.INSTRUMENTS, (
            f"{pair} is declared unavailable but present in the universe; "
            "either it now has data and should be removed from "
            "UNAVAILABLE_PAIRS, or it was added without data behind it"
        )


def test_the_named_fx_universe_adds_up():
    """17 backed pairs plus 9 unavailable ones are the 26 FX names the
    target refers to. Stated as arithmetic so a change has to be deliberate."""
    assert len(universe.FX_PAIRS) == 17
    assert len(universe.UNAVAILABLE_PAIRS) == 9
    assert len(set(universe.FX_PAIRS) | set(universe.UNAVAILABLE_PAIRS)) == 26


def test_strength_reports_what_is_unavailable_entirely():
    snap = strength.compute_strength(
        StubStore(universe.FX_PAIRS), as_of=AS_OF, window="1D"
    )
    payload = snap.as_dict()
    assert payload["pairs_unavailable"] == sorted(universe.UNAVAILABLE_PAIRS)
    # Distinct concerns: absent from this machine vs absent from this window.
    assert "pairs_missing" in payload


# ---- coverage machinery under today's universe -------------------------


def _by_currency(snap):
    return {r.currency: r for r in snap.ranking}


def test_thin_coverage_is_flagged_today_for_nzd_and_chf():
    snap = strength.compute_strength(
        StubStore(universe.FX_PAIRS), as_of=AS_OF, window="1D"
    )
    rows = _by_currency(snap)
    assert rows["NZD"].pairs_used == 1
    assert rows["NZD"].thin_coverage is True
    assert rows["CHF"].pairs_used == 2
    assert rows["CHF"].thin_coverage is True
    assert rows["USD"].thin_coverage is False


def test_a_single_pair_currency_says_it_cannot_be_cross_checked():
    snap = strength.compute_strength(
        StubStore(universe.FX_PAIRS), as_of=AS_OF, window="1D"
    )
    nzd = _by_currency(snap)["NZD"]
    assert nzd.pairs_used == 1
    # With one pair the leave-one-out delta is the whole score: dropping it
    # leaves nothing. Reported, not smoothed over.
    assert nzd.leave_one_out_max_delta == pytest.approx(abs(nzd.score))
    assert nzd.leave_one_out_driver == "NZDUSD"


# ---- the same machinery over an EXPANDED universe ----------------------


@pytest.fixture
def expanded_universe(monkeypatch):
    """Adds the nine currently-unavailable pairs, as P2-C4 eventually will."""
    added = {}
    for pair in universe.UNAVAILABLE_PAIRS:
        base, quote = pair[:3], pair[3:]
        added[pair] = universe.Instrument(
            symbol=pair, asset_class="FX", base=base, quote=quote,
            price_precision=3 if quote == "JPY" else 5,
            pip_size=0.01 if quote == "JPY" else 0.0001,
            backing=DataBacking.LIVE_ACCRUED.value,
            source_dir="live", source_stem=pair,
        )
    merged = {**universe.INSTRUMENTS, **added}
    monkeypatch.setattr(universe, "INSTRUMENTS", merged)
    monkeypatch.setattr(
        universe, "FX_PAIRS",
        tuple(s for s, m in merged.items() if m.is_fx_pair),
    )
    monkeypatch.setattr(universe, "UNAVAILABLE_PAIRS", ())
    return merged


def test_the_expanded_universe_is_actually_bigger(expanded_universe):
    assert len(universe.FX_PAIRS) == 26


def test_thin_coverage_lifts_only_where_pairs_were_genuinely_added(expanded_universe):
    snap = strength.compute_strength(
        StubStore(universe.FX_PAIRS), as_of=AS_OF, window="1D"
    )
    rows = _by_currency(snap)
    # NZD goes from one pair to five, CHF from two to five: the flag should
    # clear because the coverage really changed, not because it was dropped.
    assert rows["NZD"].pairs_used >= strength.THIN_COVERAGE_PAIRS
    assert rows["NZD"].thin_coverage is False
    assert rows["CHF"].pairs_used >= strength.THIN_COVERAGE_PAIRS
    assert rows["CHF"].thin_coverage is False


def test_the_flag_still_works_in_an_expanded_universe(expanded_universe):
    """The mechanism must remain capable of firing. A currency whose data is
    still thin keeps the flag even when the universe around it grew."""
    # Only NZDUSD has usable data; every other pair is withheld from the
    # store, so NZD is back to one usable pair inside a 26-pair universe.
    only = [p for p in universe.FX_PAIRS if "NZD" not in p] + ["NZDUSD"]
    snap = strength.compute_strength(StubStore(only), as_of=AS_OF, window="1D")
    nzd = _by_currency(snap)["NZD"]
    assert nzd.pairs_used == 1
    assert nzd.thin_coverage is True
    assert nzd.pairs_possible == 7, "possible must count the universe, not the store"
    assert nzd.coverage_ratio == pytest.approx(1 / 7)


def test_the_leave_one_out_diagnostic_survives_expansion(expanded_universe):
    """It must still single out a dominating pair once there are more of
    them -- the diagnostic is what makes a thin reading auditable."""
    drift = {p: 0.0004 for p in universe.FX_PAIRS}
    # Five times the drift of the rest, and still inside the clamp, so the
    # diagnostic has something real to single out.
    drift["NZDJPY"] = 0.002
    snap = strength.compute_strength(
        StubStore(universe.FX_PAIRS, drift), as_of=AS_OF, window="1D"
    )
    nzd = _by_currency(snap)["NZD"]
    assert nzd.pairs_used == 7
    assert nzd.leave_one_out_driver == "NZDJPY"
    assert nzd.leave_one_out_max_delta > 0


def test_expansion_does_not_silently_relabel_recorded_history(expanded_universe):
    """Pairs accrued from a feed must stay distinguishable from recorded
    ones. If expansion flattened this, a short history would read as years
    of data."""
    recorded = [s for s, m in universe.INSTRUMENTS.items()
                if m.backing == DataBacking.RECORDED_CSV.value]
    accrued = [s for s, m in universe.INSTRUMENTS.items()
               if m.backing == DataBacking.LIVE_ACCRUED.value]
    assert len(recorded) == 18
    assert len(accrued) == 9
    assert not set(recorded) & set(accrued)


def test_every_currency_still_reports_coverage_after_expansion(expanded_universe):
    snap = strength.compute_strength(
        StubStore(universe.FX_PAIRS), as_of=AS_OF, window="1D"
    )
    for row in snap.ranking:
        payload = row.as_dict()
        for key in ("pairs_used", "pairs_possible", "coverage_ratio",
                    "thin_coverage", "leave_one_out_max_delta",
                    "leave_one_out_driver", "counterparties"):
            assert key in payload, f"{row.currency} lost {key}"
        assert payload["pairs_used"] <= payload["pairs_possible"]
