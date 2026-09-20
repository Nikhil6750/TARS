"""Cheap perceptual hashing -- the Python-side half of the same technique
`chart_watcher.rs`'s `average_hash` uses (16x16 luma-weighted average
hash), needed for a real, specific correctness gap the fast chart-analysis
path (Phase D) closes:

`HotChartState` is looked up by `chart_window_id` alone at request time --
the caller cannot supply the exact symbol/timeframe identity `HotChartState.
usable_for` needs, because a fresh interactive request comes with a new
image but no exact identity yet (that only becomes known *after* a vision
call). Time-based freshness (HOT/WARM/STALE) alone cannot catch a real Part
19/26 case: the user switches from XAUUSD to EURUSD in the same window,
then immediately asks "analyze the chart" before the background watcher's
own (deliberately non-zero) vision-call cooldown has produced a fresh
EURUSD read. A HOT-by-age XAUUSD row would otherwise be served for a
EURUSD question -- exactly the cache-correctness bug Part 19 forbids.

The fix: compare a perceptual hash of the *current* request's actual
capture against the hash stored alongside the cached analysis. A real
symbol switch changes the whole chart layout/colors enough to blow past
the diff threshold; ordinary price-tick/candle updates on the *same*
symbol/timeframe do not. This is the same threshold-based judgment
`chart_watcher.rs` already makes for its own "is this worth a vision call"
decision -- applied here for "is this cached analysis still describing
what's actually on screen right now."
"""
from __future__ import annotations

from PIL import Image

HASH_GRID = 16  # 16x16 = 256-bit hash, matching chart_watcher.rs's average_hash
DEFAULT_DIFF_THRESHOLD = 14  # out of 256 bits -- same reasoning as chart_watcher.rs's HASH_DIFF_THRESHOLD


def average_hash_hex(image: Image.Image) -> str:
    """256-bit average hash of `image`, as a 64-char hex string."""
    small = image.convert("L").resize((HASH_GRID, HASH_GRID), Image.Resampling.BILINEAR)
    pixels = list(small.getdata())
    mean = sum(pixels) / len(pixels)
    bits = 0
    for i, value in enumerate(pixels):
        if value > mean:
            bits |= 1 << i
    return format(bits, "064x")


def hamming_distance_hex(hash_a: str, hash_b: str) -> int:
    return bin(int(hash_a, 16) ^ int(hash_b, 16)).count("1")


def is_same_chart_content(
    hash_a: str, hash_b: str, *, threshold: int = DEFAULT_DIFF_THRESHOLD
) -> bool:
    """True if two hashes are close enough to plausibly be the same
    chart/symbol/timeframe (allowing for ordinary price-tick/candle
    movement), False if they likely represent a real content change (e.g.
    a symbol or timeframe switch) that must not be papered over by a
    cached analysis."""
    return hamming_distance_hex(hash_a, hash_b) <= threshold
