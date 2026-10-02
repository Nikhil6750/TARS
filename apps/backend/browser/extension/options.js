async function load() {
  const { bridgePort, bridgeToken } = await chrome.storage.local.get(['bridgePort', 'bridgeToken']);
  if (bridgePort) document.getElementById('port').value = bridgePort;
  if (bridgeToken) {
    document.getElementById('token').value = bridgeToken;
    setStatus('Paired. Reconnecting in the background...', 'green');
  }
}

function setStatus(text, color) {
  const el = document.getElementById('status');
  el.textContent = text;
  el.style.color = color || '#1a1a1a';
}

document.getElementById('connect').addEventListener('click', async () => {
  const port = parseInt(document.getElementById('port').value, 10) || 17722;
  const token = document.getElementById('token').value.trim();
  if (!token) {
    setStatus('Paste the pairing token from the token file first.', 'crimson');
    return;
  }
  await chrome.storage.local.set({ bridgePort: port, bridgeToken: token });
  setStatus('Saved. Connecting...', '#1a1a1a');
  // Nudge the service worker to (re)connect now rather than waiting for its
  // own retry timer.
  chrome.runtime.sendMessage({ type: 'reconnect' }).catch(() => {});
  setTimeout(() => setStatus('Paired. If TARS still can\'t see this browser, make sure its backend is running and try again.', 'green'), 1500);
});

load();
