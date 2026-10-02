// Ported from apps/backend/browser/element_resolver.py's `_RESOLVE_FN`, same
// algorithm, adapted to be a real top-level function instead of a string
// handed to `eval()` -- `chrome.scripting.executeScript({func: ...})`
// requires an actual function reference (not a string), and running
// `eval()` of a dynamically-built string inside an arbitrary page would
// both violate many sites' Content-Security-Policy and would itself look
// exactly like the "generic remote-code-execution endpoint" TARS's own
// mission explicitly says this bridge must not expose. Keeping this as a
// named, fixed function (not reading from the websocket at all) is what
// makes "only bounded, named actions, never arbitrary code" true here.
//
// Every function in this file is self-contained (no outer closures) --
// `chrome.scripting.executeScript` serializes a function via
// `Function.prototype.toString()` and re-parses it in the target page's
// isolated world, so a reference to anything outside its own body would be
// undefined there.
function resolveAndAct(action, target, args) {
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
        const siblings = Array.from(node.parentElement.children).filter((c) => c.tagName === node.tagName);
        if (siblings.length > 1) part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
      }
      parts.unshift(part);
      if (node.id) { parts[0] = '#' + node.id; break; }
    }
    return parts.join(' > ');
  }
  function isInteractive(el) {
    const tag = el.tagName.toLowerCase();
    return ['a', 'button', 'input', 'textarea', 'select'].includes(tag) ||
           ['button', 'link', 'textbox', 'checkbox', 'radio', 'option', 'menuitem', 'tab'].includes(
             (el.getAttribute('role') || '').toLowerCase());
  }
  function describe(el, score) {
    return {
      selector: cssPath(el), tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || null,
      text: visibleText(el), name: accessibleName(el) || null, type: el.type || null,
      href: el.href || null, visible: visible(el), interactive: isInteractive(el), score: score,
    };
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
    return matches;
  }

  function act(el, sc) {
    if (action === 'find') return { ok: true, matched: describe(el, sc) };
    if (action === 'click') {
      el.scrollIntoView({ block: 'center' });
      el.focus && el.focus();
      el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
      el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
      el.click();
      return { ok: true, matched: describe(el, sc) };
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
      return { ok: true, matched: describe(el, sc), value_after: el.value || el.textContent || '' };
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
        return { ok: true, matched: describe(el, sc), value_after: el.value };
      }
      el.click();
      return { ok: true, matched: describe(el, sc) };
    }
    if (action === 'scroll_to') {
      el.scrollIntoView({ block: 'center', behavior: 'smooth' });
      return { ok: true, matched: describe(el, sc) };
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
      if (idx < 0) idx = pool.length + idx;
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
}

function extractPageText(mode) {
  if (mode === 'headings') {
    return Array.from(document.querySelectorAll('h1,h2,h3')).map((h) => h.textContent.trim()).filter(Boolean).join('\n');
  }
  let body = document.body ? (document.body.innerText || document.body.textContent || '') : '';
  body = body.trim();
  if (mode === 'summary') {
    return body.split('\n').filter((l) => l.trim().length > 0).slice(0, 20).join('\n');
  }
  return body.slice(0, 20000);
}

function extractPageLinks() {
  const seen = {};
  return Array.from(document.querySelectorAll('a[href]'))
    .map((a) => ({ text: (a.innerText || a.textContent || '').trim().slice(0, 120), href: a.href }))
    .filter((l) => l.text && l.href && !seen[l.href] && (seen[l.href] = true))
    .slice(0, 40);
}

function extractPageTable(query) {
  const all = Array.from(document.querySelectorAll('table'));
  const table = query ? (all.find((t) => (t.innerText || '').toLowerCase().includes(query.toLowerCase())) || all[0]) : all[0];
  if (!table) return { ok: false, reason: 'NOT_FOUND' };
  const rows = Array.from(table.querySelectorAll('tr')).slice(0, 50).map(
    (tr) => Array.from(tr.querySelectorAll('th,td')).map((c) => (c.innerText || '').trim())
  );
  return { ok: true, rows: rows };
}

function scrollPage(direction, amountPx) {
  if (direction === 'top') window.scrollTo({ top: 0, behavior: 'smooth' });
  else if (direction === 'bottom') window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
  else window.scrollBy({ top: direction === 'up' ? -amountPx : amountPx, behavior: 'smooth' });
  return true;
}

function pageState() {
  return { ready: document.readyState, url: document.location.href, title: document.title };
}
