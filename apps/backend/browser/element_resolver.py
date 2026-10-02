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
"""
from __future__ import annotations

import json

# One shared JS function: (action, target, args) -> JSON-serializable result.
# `action` selects what happens to the top-ranked candidate; `find` only
# resolves and reports, every other action also performs it.
#
# Candidate scoring (highest wins):
#   100  exact, case-insensitive match on visible text or accessible name
#    60  visible text/accessible name/placeholder CONTAINS the query
#    30  role matches the query (e.g. "button", "link", "textbox")
#    +20 element is interactive (button/link/input/textarea/select/[role])
#    +10 element is actually visible (laid out, non-zero size, not display:none)
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
      visible: visible(el), interactive: isInteractive(el), score: score,
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

  try {
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
    const el = top.el;
    if (action === 'find') {
      return { ok: true, matched: describe(el, top.score) };
    }
    if (action === 'click') {
      el.scrollIntoView({ block: 'center' });
      el.focus && el.focus();
      el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
      el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
      el.click();
      return { ok: true, matched: describe(el, top.score) };
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
      return { ok: true, matched: describe(el, top.score), value_after: el.value || el.textContent || '' };
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
        return { ok: true, matched: describe(el, top.score), value_after: el.value };
      }
      el.click();
      return { ok: true, matched: describe(el, top.score) };
    }
    if (action === 'scroll_to') {
      el.scrollIntoView({ block: 'center', behavior: 'smooth' });
      return { ok: true, matched: describe(el, top.score) };
    }
    return { ok: false, reason: 'UNSUPPORTED_ACTION', query: target, candidates: [] };
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
