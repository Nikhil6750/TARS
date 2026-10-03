"""AssetResolver -- the one asset-name resolution layer (mission: "THIS MUST
NOT BE XAUUSD-SPECIFIC"), used by the Universal Market Explainer and by any
voice tool that takes a spoken asset name.

Resolution order (mission section 7, followed exactly):
  1. exact currently-supported MT5 symbol (literal match against whatever
     this TARS instance actually has symbol data for right now)
  2. exact TradingView/provider symbol (folded into step 1 -- this codebase
     has no separate TradingView symbol catalog distinct from MT5's, so
     "the provider's own symbol universe" is one set, not two)
  3. known safe alias (a curated phrase -> candidate-symbol(s) table)
  4. currently active trading context (a bare pronoun -- "it"/"this" --
     resolves to whatever the caller says is currently active)
  5. AMBIGUOUS (more than one plausible candidate and nothing above
     disambiguates it) or NOT_FOUND (nothing plausible at all)

Never invents a ticker: an alias with multiple real-world broker-specific
candidates (e.g. "Nasdaq" could be US100/NAS100/USTEC/NDX depending on the
broker) resolves only when exactly one candidate matches symbols this
instance actually knows about, or there is exactly one candidate to begin
with -- otherwise it is reported AMBIGUOUS, never guessed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Outcome = Literal["RESOLVED", "AMBIGUOUS", "NOT_FOUND"]


@dataclass(frozen=True)
class ResolvedAsset:
    outcome: Outcome
    symbol: str | None = None
    candidates: tuple[str, ...] = ()
    query: str = ""


_PRONOUNS = {"it", "this", "that", "this one", "that one", "the same one"}

# phrase (normalized: lowercase, punctuation->space) -> candidate symbol(s),
# most-likely-correct first. A single-element tuple is an unambiguous alias;
# multi-element tuples are resolved against this instance's actually-known
# symbols, falling through to AMBIGUOUS when more than one still matches
# (or none of the candidates are known, in which case all are offered as
# literal candidates rather than guessing one).
_ALIASES: dict[str, tuple[str, ...]] = {
    "gold": ("XAUUSD",), "gold dollar": ("XAUUSD",),
    "silver": ("XAGUSD",), "silver dollar": ("XAGUSD",),
    "euro dollar": ("EURUSD",), "euro us dollar": ("EURUSD",), "euro": ("EURUSD",),
    "pound dollar": ("GBPUSD",), "sterling dollar": ("GBPUSD",), "cable": ("GBPUSD",),
    "dollar yen": ("USDJPY",), "yen": ("USDJPY",),
    "euro yen": ("EURJPY",), "pound yen": ("GBPJPY",),
    "aussie": ("AUDUSD",), "aussie dollar": ("AUDUSD",),
    "loonie": ("USDCAD",), "kiwi": ("NZDUSD",),
    "bitcoin": ("BTCUSD",), "btc": ("BTCUSD",),
    "ethereum": ("ETHUSD",), "ether": ("ETHUSD",), "eth": ("ETHUSD",),
    "nasdaq": ("US100", "NAS100", "USTEC", "NDX"),
    "the nasdaq": ("US100", "NAS100", "USTEC", "NDX"),
    "s p": ("SPX500", "US500", "SPY"), "s p 500": ("SPX500", "US500"),
    "sp500": ("SPX500", "US500"), "spx": ("SPX500", "US500"), "spy": ("SPY",),
    "dow": ("US30", "DJI"), "dow jones": ("US30", "DJI"),
    "dollar index": ("DXY",), "u s dollar index": ("DXY",),
    "wti": ("USOIL", "WTI", "CL"), "oil": ("USOIL", "WTI", "CL"),
    "crude oil": ("USOIL", "WTI"), "crude": ("USOIL", "WTI"),
    "brent": ("UKOIL", "BRENT"), "brent crude": ("UKOIL", "BRENT"),
    "apple": ("AAPL",), "nvidia": ("NVDA",), "tesla": ("TSLA",),
    "microsoft": ("MSFT",), "amazon": ("AMZN",), "google": ("GOOGL",), "alphabet": ("GOOGL",),
    "meta": ("META",), "facebook": ("META",),
}


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


# Every literal symbol this resolver already knows how to reach via some
# alias (e.g. "gold" -> XAUUSD means XAUUSD itself is also a recognized
# symbol) -- lets a caller that already resolved a ticker itself (e.g.
# Gemini's own trading vocabulary passing "XAUUSD" directly) still resolve
# it here even when it is not in the live `known` set, without treating
# every random uppercase word as a plausible ticker (mission: "never
# invent a ticker" -- only symbols this table already vouches for).
_ALL_ALIAS_VALUES: frozenset[str] = frozenset(c for candidates in _ALIASES.values() for c in candidates)


class AssetResolver:
    def __init__(self, monitors=None) -> None:
        self.monitors = monitors

    def _known_symbols(self) -> set[str]:
        """Every symbol this TARS instance actually has data for right now
        -- never a hardcoded "full broker catalog" this codebase has no way
        to independently verify. Deliberately conservative: an unknown-but-
        plausible alias still resolves (mission: "bitcoin" -> BTCUSD even if
        not actively polled), this set only disambiguates MULTI-candidate
        aliases."""
        known: set[str] = set()
        monitors = self.monitors
        if monitors is None:
            return known
        known |= {s.upper() for s in (getattr(monitors, "symbols", None) or [])}
        mt5 = getattr(monitors, "mt5", None)
        if mt5 is not None:
            known |= {s.upper() for s in (getattr(mt5, "symbols", None) or [])}
            try:
                known |= {s.upper() for s in (mt5.snapshot().get("quotes") or {})}
            except Exception:
                pass
        return known

    def resolve(self, raw: str, *, active_symbol: str | None = None) -> ResolvedAsset:
        text = (raw or "").strip()
        if not text:
            return ResolvedAsset("NOT_FOUND", query=raw)
        norm = _normalize(text)
        known = self._known_symbols()
        bare = norm.replace(" ", "").upper()

        # 1/2. exact currently-known symbol (literal, case-insensitive) --
        # checked BEFORE the alias table so a real, already-tracked symbol
        # always wins, and checked only against `known` (never a bare-word
        # guess: "gold" is 4 uppercase letters too, and must resolve via the
        # alias table below, not be mistaken for a literal ticker "GOLD").
        if bare and bare in known:
            return ResolvedAsset("RESOLVED", bare, query=raw)

        # 3. known alias (exact phrase, then substring, longest phrase first
        # so "s p 500" is not shadowed by a shorter accidental match). A
        # phrase like "wti" is both a spoken alias key AND one of its own
        # multi-broker candidate values, so this must run BEFORE the plain
        # alias-value fallback below -- it alone knows how to disambiguate
        # "WTI" against whichever of USOIL/WTI/CL is actually known.
        if norm in _ALIASES:
            resolved = self._resolve_candidates(_ALIASES[norm], known, raw)
            if resolved is not None:
                return resolved
        for phrase in sorted(_ALIASES, key=len, reverse=True):
            if phrase in norm:
                resolved = self._resolve_candidates(_ALIASES[phrase], known, raw)
                if resolved is not None:
                    return resolved

        # 3b. not a known symbol and not matched via any alias phrase, but
        # the literal text is itself a symbol this table already vouches
        # for as *someone's* alias destination (e.g. "XAUUSD"/"NVDA"/
        # "GBPJPY" typed directly, as Gemini's own trading vocabulary
        # already does for majors) -- resolves even when not currently
        # monitored. Deliberately after the alias-phrase step above, not
        # before: a multi-candidate case like "wti" must still go through
        # _resolve_candidates' known-set disambiguation rather than always
        # resolving to itself.
        if bare and bare in _ALL_ALIAS_VALUES:
            return ResolvedAsset("RESOLVED", bare, query=raw)

        # 4. currently active trading context (pronoun-only reference).
        if norm in _PRONOUNS:
            if active_symbol:
                return ResolvedAsset("RESOLVED", active_symbol.upper(), query=raw)
            return ResolvedAsset("NOT_FOUND", query=raw)

        # Deliberately no further fallback: an unrecognized phrase that is
        # neither a known symbol nor a known alias is reported NOT_FOUND,
        # never guessed at from its shape alone (mission: "never invent a
        # ticker").
        return ResolvedAsset("NOT_FOUND", query=raw)

    @staticmethod
    def _resolve_candidates(candidates: tuple[str, ...], known: set[str], raw: str) -> ResolvedAsset | None:
        if len(candidates) == 1:
            return ResolvedAsset("RESOLVED", candidates[0], query=raw)
        matching_known = [c for c in candidates if c in known]
        if len(matching_known) == 1:
            return ResolvedAsset("RESOLVED", matching_known[0], query=raw)
        if len(matching_known) > 1:
            return ResolvedAsset("AMBIGUOUS", candidates=tuple(matching_known), query=raw)
        # None of the candidates are currently known -- still ambiguous
        # (never guess one), but offer all literal candidates for context.
        return ResolvedAsset("AMBIGUOUS", candidates=candidates, query=raw)
