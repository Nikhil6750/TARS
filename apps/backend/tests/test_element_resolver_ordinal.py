"""Pure-Python ordinal/positional query parsing (browser/element_resolver.py's
`parse_ordinal`) -- no browser needed, this is the half of ordinal
resolution that's actually unit-testable; the DOM-grouping half is exercised
live in tests/test_browser_live_acceptance.py."""
from __future__ import annotations

from browser.element_resolver import parse_ordinal


def test_plain_text_target_has_no_ordinal():
    q = parse_ordinal("sign in")
    assert q.index is None and q.relative is None and not q.is_pronoun


def test_first_result():
    q = parse_ordinal("the first result")
    assert q.index == 0
    assert q.hint is None  # "result" is a stopword -- it's not a useful DOM hint on its own


def test_second_video():
    q = parse_ordinal("the second video")
    assert q.index == 1
    # "video" is a generic collection noun (what KIND of thing, not literal
    # text to filter by) -- hint=None falls back to the default visible-link
    # pool, which is what actually resolves real video titles on YouTube
    # (confirmed live: filtering by literal "video" text matches nothing).
    assert q.hint is None


def test_third_download_button():
    q = parse_ordinal("the third download button")
    assert q.index == 2
    assert q.hint == "download"  # "button" is a stopword


def test_last_tab():
    q = parse_ordinal("the last tab")
    assert q.index == -1
    assert q.hint == "tab"


def test_top_result_and_bottom_result():
    assert parse_ordinal("the top result").index == 0
    assert parse_ordinal("the bottom result").index == -1


def test_next_one_is_relative_not_absolute():
    q = parse_ordinal("the next one")
    assert q.index is None
    assert q.relative == "next"
    assert q.hint is None


def test_previous_one_is_relative():
    q = parse_ordinal("the previous one")
    assert q.relative == "previous"


def test_pronoun_only_references():
    for phrase in ("it", "that", "this", "that one", "this one", " It ", "THAT"):
        q = parse_ordinal(phrase)
        assert q.is_pronoun, phrase
        assert q.index is None and q.relative is None


def test_ordinal_word_as_a_substring_of_an_unrelated_word_is_not_misparsed():
    # "firstName" shouldn't trigger on "first" -- word-boundary matching.
    q = parse_ordinal("the firstName field")
    assert q.index is None


def test_is_positional_property():
    assert parse_ordinal("the first result").is_positional
    assert parse_ordinal("the next one").is_positional
    assert parse_ordinal("it").is_positional
    assert not parse_ordinal("sign in").is_positional


def test_numeric_ordinal_words():
    assert parse_ordinal("the 1st link").index == 0
    assert parse_ordinal("the 3rd item").index == 2
