"""AssetResolver -- mission section 7's exact examples, plus the resolution
order it specifies (exact known symbol -> alias -> active context ->
AMBIGUOUS/NOT_FOUND). Pure logic, no monitors needed except a tiny fake
exposing `.symbols`/`.mt5.symbols`/`.mt5.snapshot()`.
"""
from __future__ import annotations

from trading.asset_resolver import AssetResolver, ResolvedAsset


class _FakeMT5:
    def __init__(self, symbols, quotes=None):
        self.symbols = symbols
        self._quotes = quotes or {}

    def snapshot(self):
        return {"quotes": self._quotes}


class _FakeMonitors:
    def __init__(self, symbols, mt5_symbols=None, quotes=None):
        self.symbols = symbols
        self.mt5 = _FakeMT5(mt5_symbols or symbols, quotes)


def _resolver(symbols=("EURUSD", "XAUUSD")) -> AssetResolver:
    return AssetResolver(_FakeMonitors(list(symbols)))


def test_gold_resolves_to_xauusd():
    out = _resolver().resolve("gold")
    assert out == ResolvedAsset("RESOLVED", "XAUUSD", query="gold")


def test_literal_known_symbol_resolves_directly():
    out = _resolver().resolve("EURUSD")
    assert out.outcome == "RESOLVED" and out.symbol == "EURUSD"


def test_literal_symbol_is_case_insensitive_and_tolerates_spacing():
    out = _resolver().resolve(" eurusd ")
    assert out.outcome == "RESOLVED" and out.symbol == "EURUSD"


def test_bitcoin_resolves_to_btcusd_even_when_not_actively_tracked():
    # Mission: "bitcoin -> resolve to the actual BTC instrument supported
    # by the active provider" -- a single-candidate alias resolves even if
    # BTCUSD isn't in the currently-polled symbol list.
    out = _resolver(symbols=("EURUSD", "XAUUSD")).resolve("bitcoin")
    assert out == ResolvedAsset("RESOLVED", "BTCUSD", query="bitcoin")


def test_ethereum_alias():
    assert _resolver().resolve("ethereum").symbol == "ETHUSD"


def test_euro_dollar_phrase():
    assert _resolver().resolve("euro dollar").symbol == "EURUSD"


def test_nasdaq_is_ambiguous_with_no_disambiguating_context():
    # Mission's own example: multiple plausible broker symbols, none
    # actively known -> AMBIGUOUS, never a guess.
    out = _resolver().resolve("Nasdaq")
    assert out.outcome == "AMBIGUOUS"
    assert set(out.candidates) == {"US100", "NAS100", "USTEC", "NDX"}


def test_nasdaq_resolves_when_exactly_one_candidate_is_actually_known():
    out = AssetResolver(_FakeMonitors(["EURUSD", "NAS100"])).resolve("nasdaq")
    assert out == ResolvedAsset("RESOLVED", "NAS100", query="nasdaq")


def test_nasdaq_still_ambiguous_when_two_candidates_are_known():
    out = AssetResolver(_FakeMonitors(["US100", "NAS100"])).resolve("nasdaq")
    assert out.outcome == "AMBIGUOUS"
    assert set(out.candidates) == {"US100", "NAS100"}


def test_unrecognized_phrase_is_not_found_never_guessed():
    out = _resolver().resolve("purple elephant currency")
    assert out == ResolvedAsset("NOT_FOUND", query="purple elephant currency")


def test_plain_word_that_is_also_an_alias_key_does_not_collide_with_literal_symbol_guessing():
    # "gold" is 4 uppercase letters -- must resolve via the alias table
    # (XAUUSD), never be treated as a literal ticker "GOLD".
    out = _resolver().resolve("gold")
    assert out.symbol == "XAUUSD"


def test_pronoun_resolves_to_active_context():
    out = _resolver().resolve("it", active_symbol="eurusd")
    assert out == ResolvedAsset("RESOLVED", "EURUSD", query="it")

    out2 = _resolver().resolve("this one", active_symbol="XAUUSD")
    assert out2.symbol == "XAUUSD"


def test_pronoun_without_active_context_is_not_found():
    out = _resolver().resolve("it")
    assert out.outcome == "NOT_FOUND"


def test_empty_input_is_not_found():
    assert _resolver().resolve("").outcome == "NOT_FOUND"
    assert _resolver().resolve("   ").outcome == "NOT_FOUND"


def test_substring_alias_match_inside_a_longer_phrase():
    out = _resolver().resolve("what's going on in gold today")
    assert out.symbol == "XAUUSD"


def test_equity_tickers():
    assert _resolver().resolve("Nvidia").symbol == "NVDA"
    assert _resolver().resolve("apple").symbol == "AAPL"
    assert _resolver().resolve("tesla").symbol == "TSLA"


def test_oil_variants():
    assert _resolver().resolve("oil").outcome == "AMBIGUOUS" or _resolver().resolve("oil").symbol in ("USOIL", "WTI", "CL")
    out = AssetResolver(_FakeMonitors(["EURUSD", "USOIL"])).resolve("WTI")
    assert out.symbol == "USOIL"


def test_resolver_works_with_no_monitors_at_all():
    out = AssetResolver(None).resolve("gold")
    assert out.symbol == "XAUUSD"
    out2 = AssetResolver(None).resolve("nasdaq")
    assert out2.outcome == "AMBIGUOUS"
