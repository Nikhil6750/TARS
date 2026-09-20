import React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './App';
import './styles/index.css';

// The desktop app must never be served from a stale service-worker cache (the PWA precache
// kept an old UI bundle alive across rebuilds). Web/PWA builds keep their worker.
if ('__TAURI_INTERNALS__' in window && 'serviceWorker' in navigator) {
  void navigator.serviceWorker.getRegistrations().then(list => list.forEach(r => void r.unregister()));
  void caches?.keys().then(keys => keys.forEach(k => void caches.delete(k)));
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
