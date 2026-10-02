"""Semantic element resolution for the real-browser agent.

Mirrors the ranking approach `apps/web/src/services/browser-control.ts`
already uses for TARS's embedded dashboard (`findElement`/`SemanticCriteria`)
-- visible text, accessible name, role, placeholder, in that order of
preference, interactive elements favored over inert ones -- so both control
surfaces resolve "click login" the same way. This module targets a real page
via CDP instead: the ranking runs as one JS payload executed through
`browser/cdp.py`'s `evaluate()`, so resolution and the resulting action
(click/type/select/scroll/read) happen in a single round trip against a
single, consistent DOM snapshot rather than two round trips that could race
a page re-render in between.

`_RESOLVE_FN` is a JS expression, not a template to fill with untrusted
values -- every call site passes the user-provided target text and action
arguments as a separate JSON-encoded argument to the invocation, never
string-interpolated into the function body, so page content/voice transcript
text can never break out of its argument position.

Ordinal/positional resolution ("the first result", "the second video", "the
last tab", "the top one"): `parse_ordinal(target)` is the NLP-ish half of
this -- pure Python, no DOM needed, fully unit-testable -- and produces an
`OrdinalQuery` the JS side turns into an actual DOM index. Splitting it this
way means the part that is genuinely hard to unit test (DOM grouping) is as
small and as reused as possible, and the part that drives most of the
correctness (recognizing "first"/"second"/.../"last"/"next"/"previous" and
pulling out the group noun, e.g. "video" out of "the second video") is
exercised directly against strings, not against a real page.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

_ORDINAL_WORDS: dict[str, int] = {
    "first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2,
    "fourth": 3, "4th": 3, "fifth": 4, "5th": 4, "sixth": 5, "6th": 5,
    "seventh": 6, "7th": 6, "eighth": 7, "8th": 7, "ninth": 8, "9th": 8,
    "tenth": 9, "10th": 9, "top": 0, "last": -1, "bottom": -1,
}
_RELATIVE_WORDS = {"next": 1, "another": 1, "previous": -1, "prior": -1}
# Pure pronoun references with no ordinal/descriptive noun at all -- resolved
# against the *last successfully acted-on element*, re-found fresh (see
# BrowserSession's module docstring), never a cached DOM node.
_PRONOUN_ONLY = re.compile(r"^\s*(it|that|this|that one|this one|the same one)\s*$", re.IGNORECASE)
# Generic collection nouns -- "the first VIDEO"/"the second RESULT" name
# what KIND of thing to count positionally, not literal text to filter by
# (confirmed live: filtering by hint="video" excludes every real YouTube
# video link, since titles don't contain the literal word "video"). Treated
# the same as "result" was always treated: stripped, leaving hint=None so
# the ordinal falls back to the plain ranked/visible candidate pool. A more
# specific hint ("the download button", "the Formula 1 one") still filters.
_STOPWORDS = {
    "the", "a", "an", "on", "page", "one",
    "button", "buttons", "link", "links", "result", "results", "item", "items",
    "video", "videos", "article", "articles", "post", "posts", "entry", "entries",
    "row", "rows", "option", "options", "choice", "choices", "thing", "things",
}
_ORDINAL_OR_RELATIVE_RE = re.compile(
    r"\b(" + "|".join(sorted(set(_ORDINAL_WORDS) | set(_RELATIVE_WORDS), key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class OrdinalQuery:
    """Result of parsing a target phrase for ordinal/relative/positional
    meaning. `index` is 0-based (negative = count-from-the-end, e.g. -1 =
    last), or None if the phrase had no ordinal/relative/positional word at
    all (the existing plain-text-match path handles those unchanged)."""

    index: int | None
    relative: str | None  # "next" | "previous" | None
    hint: str | None      # the remaining descriptive noun, e.g. "video", "download button"
    is_pronoun: bool
    raw: str

    @property
    def is_positional(self) -> bool:
        return self.index is not None or self.relative is not None or self.is_pronoun


def parse_ordinal(target: str) -> OrdinalQuery:
    raw = target or ""
    if _PRONOUN_ONLY.match(raw):
        return OrdinalQuery(index=None, relative=None, hint=None, is_pronoun=True, raw=raw)

    match = _ORDINAL_OR_RELATIVE_RE.search(raw)
    if not match:
        return OrdinalQuery(index=None, relative=None, hint=None, is_pronoun=False, raw=raw)

    word = match.group(1).lower()
    index = _ORDINAL_WORDS.get(word)
    relative = "next" if word in ("next", "another") else ("previous" if word in ("previous", "prior") else None)

    remainder = (raw[: match.start()] + " " + raw[match.end() :]).strip()
    words = [w for w in re.findall(r"[a-z0-9]+", remainder.lower()) if w not in _STOPWORDS]
    hint = " ".join(words) if words else None

    return OrdinalQuery(index=index, relative=relative, hint=hint, is_pronoun=False, raw=raw)


# One shared JS function: (action, target, args) -> JSON-serializable result.
# `action` selects what happens to the top-ranked candidate; `find` only
# resolves and reports, every other action also performs it.
#
# Two selection modes, chosen by whether `args.ordinal` is present:
#
#  * Plain text match (ordinal absent, the original/default path) -- scored:
#      100  exact, case-insensitive match on visible text or accessible name
#       60  visible text/accessible name/placeholder CONTAINS the query
#       30  role matches the query (e.g. "button", "link", "textbox")
#      +20  element is interactive (button/link/input/textarea/select/[role])
#      +10  element is actually visible (laid out, non-zero size, not hidden)
#
#  * Ordinal/positional ("the second video", "the last one") -- `args.ordinal`
#    is `{index, hint}` (see `parse_ordinal` above). Candidates are every
#    visible interactive element (anchors/buttons/role=button|link), filtered
#    to ones whose text/accessible-name/role contains `hint` when a hint was
#    given, kept in real DOM/visual document order (not scored), then indexed
#    by `index` (negative counts from the end). This deliberately does not
#    try to detect "the search-results card" as a special DOM shape -- on
#    real results pages (Google, YouTube, ...) the matching elements already
#    occur in the right reading order, so indexing the filtered, ordered list
#    directly resolves "first"/"second"/"last" correctly without inventing a
#    generic, unverifiable clustering heuristic.
#
# A candidate must be visible to be actionable; invisible candidates are
# still returned for `find` (useful diagnostics) but never acted on.
_RESOLVE_FN = r"""
(function(action, target, args) {
  function visible(el) {
    if (!el || !el.isConnected) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    const style = window.getComputedStyle(el);
    return style.visibility !== 'hidden' && style.display !== 'none' && +style.opacity !== 0;
  }
  function accessibleName(el) {
    return (el.getAttribute('aria-label') || el.getAttribute('alt') ||
            el.getAttribute('title') || el.getAttribute('placeholder') || '').trim();
  }
  function visibleText(el) {
    return (el.innerText || el.textContent || el.value || '').trim().replace(/\s+/g, ' ').slice(0, 200);
  }
  function cssPath(el) {
    if (el.id) return '#' + el.id;
    const parts = [];
    let node = el;
    for (let depth = 0; node && node.nodeType === 1 && depth < 6; depth++, node = node.parentElement) {
      let part = node.tagName.toLowerCase();
      if (node.parentElement) {
        const siblings = Array.from(node.parentElement.children).filter(c => c.tagName === node.tagName);
        if (siblings.length > 1) part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
      }
      parts.unshift(part);
      if (node.id) { parts[0] = '#' + node.id; break; }
    }
    return parts.join(' > ');
  }
  function describe(el, score) {
    return {
      selector: cssPath(el), tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || null,
      text: visibleText(el), name: accessibleName(el) || null, type: el.type || null,
      href: el.href || null, visible: visible(el), interactive: isInteractive(el), score: score,
    };
  }
  function isInteractive(el) {
    const tag = el.tagName.toLowerCase();
    return ['a', 'button', 'input', 'textarea', 'select'].includes(tag) ||
           ['button', 'link', 'textbox', 'checkbox', 'radio', 'option', 'menuitem', 'tab'].includes(
             (el.getAttribute('role') || '').toLowerCase());
  }
  function score(el, query) {
    if (!query) return isInteractive(el) ? 20 : 0;
    const q = query.trim().toLowerCase();
    const text = visibleText(el).toLowerCase();
    const name = accessibleName(el).toLowerCase();
    const role = (el.getAttribute('role') || el.tagName.toLowerCase()).toLowerCase();
    let s = 0;
    if (text === q || name === q) s = 100;
    else if (text.includes(q) || name.includes(q)) s = 60;
    else if (role === q) s = 30;
    else return 0;
    if (isInteractive(el)) s += 20;
    if (visible(el)) s += 10;
    return s;
  }
  function candidates(query) {
    const nodes = document.querySelectorAll(
      'a[href], button, input, textarea, select, [role], h1, h2, h3, [onclick], label'
    );
    const scored = [];
    nodes.forEach((el) => {
      const sc = score(el, query);
      if (sc > 0) scored.push({ el: el, score: sc });
    });
    scored.sort((a, b) => b.score - a.score);
    return scored;
  }
  function ordinalCandidates(hint) {
    // Scoped to the page's main-content landmark when it declares one
    // (confirmed live on real Google/YouTube results pages -- both expose
    // exactly one <main>/[role=main]), and chrome (header/nav/footer) is
    // excluded even then, as defense in depth for pages that nest nav
    // inside main. Without a hint ("the first result"/"the top one"),
    // restricted to real links only -- unhinted "a result" overwhelmingly
    // means a link to content, not a toolbar/filter button (confirmed
    // live: YouTube's filter chips -- "All", "Shorts", "Live" -- are
    // <button> elements inside <main> that would otherwise outrank the
    // actual first video). A hint ("the first download button") still
    // allows buttons, since the user named one.
    const scope = document.querySelector('main, [role="main"]') || document;
    const selector = hint ? 'a[href], button, [role="button"], [role="link"]' : 'a[href]';
    const nodes = scope.querySelectorAll(selector);
    const q = (hint || '').trim().toLowerCase();
    const matches = [];
    nodes.forEach((el) => {
      if (!visible(el)) return;
      if (el.closest('header, nav, footer, [role="banner"], [role="navigation"], [role="contentinfo"]')) return;
      if (q) {
        const text = visibleText(el).toLowerCase();
        const name = accessibleName(el).toLowerCase();
        const role = (el.getAttribute('role') || el.tagName.toLowerCase()).toLowerCase();
        if (!text.includes(q) && !name.includes(q) && role !== q) return;
      }
      matches.push(el);
    });
    return matches; // already in document order, which querySelectorAll guarantees
  }

  function act(el, score) {
    if (action === 'find') {
      return { ok: true, matched: describe(el, score) };
    }
    if (action === 'click') {
      el.scrollIntoView({ block: 'center' });
      el.focus && el.focus();
      el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
      el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
      el.click();
      return { ok: true, matched: describe(el, score) };
    }
    if (action === 'type') {
      const text = args.text || '';
      el.scrollIntoView({ block: 'center' });
      el.focus();
      if (args.clear !== false && 'value' in el) el.value = '';
      if ('value' in el) el.value = (el.value || '') + text;
      else el.textContent = text;
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      if (args.submit) {
        const form = el.form || el.closest('form');
        el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
        if (form && form.requestSubmit) form.requestSubmit();
      }
      return { ok: true, matched: describe(el, score), value_after: el.value || el.textContent || '' };
    }
    if (action === 'select') {
      el.scrollIntoView({ block: 'center' });
      const value = args.value;
      if (el.tagName.toLowerCase() === 'select') {
        const opt = Array.from(el.options).find(
          (o) => o.value === value || o.textContent.trim().toLowerCase() === String(value).toLowerCase());
        if (!opt) return { ok: false, reason: 'NOT_FOUND', query: 'option:' + value, candidates: [] };
        el.value = opt.value;
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return { ok: true, matched: describe(el, score), value_after: el.value };
      }
      el.click();
      return { ok: true, matched: describe(el, score) };
    }
    if (action === 'scroll_to') {
      el.scrollIntoView({ block: 'center', behavior: 'smooth' });
      return { ok: true, matched: describe(el, score) };
    }
    return { ok: false, reason: 'UNSUPPORTED_ACTION', query: target, candidates: [] };
  }

  try {
    const ordinal = args && args.ordinal;
    if (ordinal) {
      const pool = ordinalCandidates(ordinal.hint);
      const diagnostics = { matches: pool.length, group: ordinal.hint || '(any)' };
      if (pool.length === 0) {
        return { ok: false, reason: 'NOT_FOUND', query: target, candidates: [], diagnostics: diagnostics };
      }
      let idx = ordinal.index;
      if (idx < 0) idx = pool.length + idx; // -1 == last
      if (idx < 0 || idx >= pool.length) {
        return {
          ok: false, reason: 'NOT_FOUND', query: target,
          candidates: pool.slice(0, 5).map((el) => describe(el, 0)),
          diagnostics: Object.assign(diagnostics, { ordinal: ordinal.index, out_of_range: true }),
        };
      }
      const result = act(pool[idx], 100);
      if (result.ok) result.diagnostics = Object.assign(diagnostics, { ordinal: idx, selected: result.matched });
      return result;
    }

    const scored = candidates(target || '');
    const actionable = scored.filter((c) => visible(c.el));
    if (actionable.length === 0) {
      return { ok: false, reason: 'NOT_FOUND', query: target,
               candidates: scored.slice(0, 5).map((c) => describe(c.el, c.score)) };
    }
    const top = actionable[0];
    const runnerUp = actionable[1];
    if (runnerUp && runnerUp.score === top.score && top.score < 100) {
      return { ok: false, reason: 'AMBIGUOUS', query: target,
               candidates: actionable.slice(0, 5).map((c) => describe(c.el, c.score)) };
    }
    return act(top.el, top.score);
  } catch (err) {
    return { ok: false, reason: 'SCRIPT_ERROR', query: target, candidates: [], error: String(err) };
  }
})
"""


def build_invocation(action: str, target: str, args: dict | None = None) -> str:
    """Returns the full JS expression to hand to `browser/cdp.py`'s `evaluate()`
    for one resolve-and-act call. `action`/`target`/`args` are JSON-encoded as
    call arguments (not interpolated into source) so page/voice text can
    never be interpreted as script."""
    return f"({_RESOLVE_FN})({json.dumps(action)}, {json.dumps(target)}, {json.dumps(args or {})})"
