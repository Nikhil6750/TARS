// TARS Browser Bridge -- background service worker.
//
// Security (mission section 6): this file only ever opens an OUTBOUND
// websocket to ws://127.0.0.1:<port>, authenticated with a token the person
// pastes in from a local file during one-time setup (see options.html) --
// no website's page JS can reach this at all (there is no listener any page
// could call into), and the local server independently checks both that
// token and this extension's own chrome-extension:// origin before acting
// on anything it receives (see bridge_server.py). The only actions this
// file will run are the named ones in `ACTIONS` below, each a bounded,
// specific capability (list tabs, click a described element, read text,
// start a download, ...) -- never a generic "run this JS" endpoint.
importScripts('resolver.js');

const RECONNECT_DELAY_MS = 3000;
let ws = null;
let reconnectTimer = null;

async function getPairing() {
  const { bridgePort, bridgeToken } = await chrome.storage.local.get(['bridgePort', 'bridgeToken']);
  return { port: bridgePort || 17722, token: bridgeToken || null };
}

async function connect() {
  const { port, token } = await getPairing();
  if (!token) return; // not paired yet -- options.html walks the person through it
  if (ws && ws.readyState === WebSocket.OPEN) return;

  ws = new WebSocket(`ws://127.0.0.1:${port}/bridge`);
  ws.onopen = () => {
    ws.send(JSON.stringify({ type: 'hello', token }));
  };
  ws.onmessage = (event) => handleMessage(event.data);
  ws.onclose = ws.onerror = () => {
    ws = null;
    scheduleReconnect();
  };
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, RECONNECT_DELAY_MS);
}

async function handleMessage(raw) {
  let message;
  try {
    message = JSON.parse(raw);
  } catch {
    return;
  }
  if (message.type === 'hello_ack') return;
  if (message.type !== 'cmd') return;

  let result;
  try {
    result = await dispatch(message.action, message.args || {});
  } catch (err) {
    ws.send(JSON.stringify({ type: 'error', id: message.id, error: String(err) }));
    return;
  }
  ws.send(JSON.stringify({ type: 'result', id: message.id, result: result }));
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab) throw new Error('no active tab');
  return tab;
}

async function execIn(tabId, func, args) {
  const [{ result }] = await chrome.scripting.executeScript({ target: { tabId }, func, args });
  return result;
}

// ACTIONS: the complete, bounded capability surface this bridge exposes --
// matches skills/web_browser.py's own action set 1:1 (mission section 8:
// "upper-level tools must not care" which provider executed them) so a
// request routed here behaves like the CDP path for the caller.
const ACTIONS = {
  async get_context() {
    const tab = await activeTab();
    const tabs = await chrome.tabs.query({});
    return { url: tab.url, title: tab.title, tab_count: tabs.length, active_tab_index: tabs.findIndex((t) => t.id === tab.id), active_tab_id: String(tab.id) };
  },
  async list_tabs() {
    const tabs = await chrome.tabs.query({});
    return tabs.map((t, i) => ({ index: i, id: String(t.id), url: t.url, title: t.title, active: t.active }));
  },
  async focus_tab({ target }) {
    const tabs = await chrome.tabs.query({});
    const match = matchTab(tabs, target);
    if (!match) return { ok: false, reason: 'NOT_FOUND', query: target };
    await chrome.tabs.update(match.id, { active: true });
    await chrome.windows.update(match.windowId, { focused: true });
    return { ok: true, id: String(match.id), url: match.url, title: match.title };
  },
  async new_tab({ url }) {
    const tab = await chrome.tabs.create({ url: url || undefined });
    return { ok: true, id: String(tab.id), url: tab.url, title: tab.title };
  },
  async close_tab({ target }) {
    const tabs = await chrome.tabs.query({});
    const match = target ? matchTab(tabs, target) : await activeTab();
    if (!match) return { ok: false, reason: 'NOT_FOUND', query: target };
    await chrome.tabs.remove(match.id);
    return { ok: true, closed_id: String(match.id) };
  },
  async navigate({ url }) {
    const tab = await activeTab();
    const before = tab.url;
    await chrome.tabs.update(tab.id, { url });
    const ready = await waitForTabReady(tab.id, 12000);
    const after = await chrome.tabs.get(tab.id);
    return { ok: ready, url: after.url || url, title: after.title, requested: url };
  },
  async back() { return historyNav(-1); },
  async forward() { return historyNav(1); },
  async refresh() {
    const tab = await activeTab();
    await chrome.tabs.reload(tab.id);
    const ready = await waitForTabReady(tab.id, 12000);
    const after = await chrome.tabs.get(tab.id);
    return { ok: ready, url: after.url, title: after.title };
  },
  async resolve({ js_action, target, args }) {
    const tab = await activeTab();
    return await execIn(tab.id, resolveAndAct, [js_action, target, args || {}]);
  },
  async extract_text({ mode }) {
    const tab = await activeTab();
    const text = await execIn(tab.id, extractPageText, [mode || 'summary']);
    return { ok: true, mode: mode || 'summary', text: text || '' };
  },
  async get_links() {
    const tab = await activeTab();
    const links = await execIn(tab.id, extractPageLinks, []);
    return { ok: true, links: links || [] };
  },
  async extract_table({ target }) {
    const tab = await activeTab();
    return await execIn(tab.id, extractPageTable, [target || '']);
  },
  async scroll({ direction, amount }) {
    const tab = await activeTab();
    const px = amount === 'large' ? 1200 : 400;
    await execIn(tab.id, scrollPage, [direction || 'down', px]);
    return { ok: true, direction: direction || 'down', amount: amount || 'small' };
  },
  // Deliberately NOT a self-contained "download" action that also does the
  // click: the click needs ordinal/relative/pronoun resolution first (mission
  // sections 1-2, "download the first PDF"), which is Python-side logic the
  // CDP and bridge providers share (browser/resolution.py) -- this action
  // only watches for the download chrome.downloads already knows about,
  // after BridgeSession's own `click()` (same ordinal-aware path as every
  // other click) has run.
  async wait_for_download({ source_url, timeout_ms }) {
    const item = await waitForDownload(source_url, timeout_ms || 30000);
    if (!item) return { ok: false, status: 'FAILED', reason: 'NO_DOWNLOAD_STARTED', source_url };
    if (item.state === 'complete') {
      return { ok: true, status: 'COMPLETED', filename: item.filename, path: item.filename, size_bytes: item.fileSize || item.bytesReceived || 0, source_url };
    }
    return { ok: true, status: 'DOWNLOADING', source_url };
  },
};

function matchTab(tabs, query) {
  if (!query) return null;
  const byId = tabs.find((t) => String(t.id) === query);
  if (byId) return byId;
  if (/^\d+$/.test(query)) {
    const idx = parseInt(query, 10);
    return tabs[idx] || null;
  }
  const needle = query.toLowerCase();
  return tabs.find((t) => (t.title || '').toLowerCase().includes(needle) || (t.url || '').toLowerCase().includes(needle)) || null;
}

async function historyNav(delta) {
  const tab = await activeTab();
  const before = tab.url;
  await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: (d) => { d < 0 ? history.back() : history.forward(); }, args: [delta] });
  const ready = await waitForTabReady(tab.id, 8000);
  const after = await chrome.tabs.get(tab.id);
  return { ok: ready && after.url !== before, url: after.url, title: after.title, changed: after.url !== before };
}

function waitForTabReady(tabId, timeoutMs) {
  return new Promise((resolve) => {
    const deadline = Date.now() + timeoutMs;
    const check = async () => {
      try {
        const tab = await chrome.tabs.get(tabId);
        if (tab.status === 'complete') { resolve(true); return; }
      } catch { resolve(false); return; }
      if (Date.now() > deadline) { resolve(false); return; }
      setTimeout(check, 300);
    };
    check();
  });
}

function waitForDownload(sourceUrl, timeoutMs) {
  return new Promise((resolve) => {
    const deadline = Date.now() + timeoutMs;
    const check = () => {
      chrome.downloads.search({ url: sourceUrl, limit: 1, orderBy: ['-startTime'] }, (items) => {
        const item = items && items[0];
        if (item && (item.state === 'complete' || item.state === 'interrupted')) { resolve(item); return; }
        if (Date.now() > deadline) { resolve(item || null); return; }
        setTimeout(check, 400);
      });
    };
    check();
  });
}

async function dispatch(action, args) {
  const handler = ACTIONS[action];
  if (!handler) return { ok: false, reason: 'UNSUPPORTED_ACTION', action };
  return await handler(args);
}

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
chrome.runtime.onMessage.addListener((message) => {
  if (message && message.type === 'reconnect') connect();
});
connect();
