/**
 * Where this page's backend lives.
 *
 * Inside the desktop shell the address is injected before any page script
 * runs (`backend_endpoint.rs`), so the webviews always talk to the backend
 * the supervisor is actually managing. Outside it, `VITE_TARS_API_URL`
 * applies, then the default. This is the only place the default is written.
 */
const DEFAULT_BACKEND_URL = 'http://127.0.0.1:8000';

const trim = (url: string): string => url.replace(/\/$/, '');

function injected(): string | null {
  const value = (globalThis as { __TARS_BACKEND_URL__?: unknown }).__TARS_BACKEND_URL__;
  return typeof value === 'string' && value ? trim(value) : null;
}

export function backendHttpUrl(): string {
  const shell = injected();
  if (shell) return shell;
  const env = (import.meta as unknown as { env?: Record<string, string> }).env;
  return env?.VITE_TARS_API_URL ? trim(String(env.VITE_TARS_API_URL)) : DEFAULT_BACKEND_URL;
}

/** The same address, as a WebSocket URL. */
export function backendWsUrl(path = ''): string {
  return `${backendHttpUrl().replace(/^http/, 'ws')}${path}`;
}

/**
 * An endpoint saved in settings on an earlier run must not outrank the shell:
 * a remote (Tailscale) address is a user choice and is kept, but a saved
 * loopback address is only a remembered default, and following it would point
 * the UI at a port nothing is serving.
 */
export function followShellEndpoint(saved: string, kind: 'http' | 'ws'): string {
  const shell = injected();
  if (!shell || !/^(http|ws):\/\/(127\.0\.0\.1|localhost)(:|\/|$)/.test(saved)) return saved;
  if (kind === 'http') return shell;
  return backendWsUrl(new URL(saved).pathname);
}
