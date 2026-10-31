/**
 * Desktop integration for the Market Intelligence window.
 *
 * The dashboard is a Tauri window in normal use, but it must also render in a
 * plain browser for development and verification. Everything here degrades to
 * a no-op off the desktop shell rather than throwing, so a single codebase
 * serves both without branching through the UI.
 *
 * Preferences are stored locally on this machine via `localStorage`, which in
 * the Tauri webview is a per-application store under the app's own data
 * directory. Reads and writes are wrapped because storage can be unavailable
 * or throw (private mode, cleared site data), and the dashboard must still
 * paint with its defaults when it is.
 */

interface TauriInternals {
  invoke: <T>(cmd: string, args?: Record<string, unknown>) => Promise<T>;
}

function internals(): TauriInternals | null {
  const win = window as unknown as {
    __TAURI_INTERNALS__?: TauriInternals;
    __TAURI__?: { core?: TauriInternals };
  };
  return win.__TAURI_INTERNALS__ ?? win.__TAURI__?.core ?? null;
}

export const isDesktop = (): boolean => internals() !== null;

async function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T | null> {
  const api = internals();
  if (!api) return null;
  try {
    return await api.invoke<T>(cmd, args);
  } catch (error) {
    console.warn(`[market] desktop command ${cmd} failed:`, error);
    return null;
  }
}

/** Toggles native fullscreen. Returns the new state, or null off the desktop. */
export const toggleFullscreen = (): Promise<boolean | null> =>
  invoke<boolean>('market_toggle_fullscreen');

/* ---------- local preferences ---------- */

export interface Preferences {
  timeframe: string;
  symbol: string | null;
  strengthWindow: string;
  showInvalidated: boolean;
}

export const DEFAULT_PREFERENCES: Preferences = {
  timeframe: 'H1',
  symbol: null,
  strengthWindow: '1D',
  showInvalidated: false
};

const PREFS_KEY = 'tars.market-xray.preferences.v1';

export function loadPreferences(): Preferences {
  try {
    const raw = window.localStorage.getItem(PREFS_KEY);
    if (!raw) return { ...DEFAULT_PREFERENCES };
    const parsed = JSON.parse(raw) as Partial<Preferences>;
    // Validate rather than trust: a stale or hand-edited value must not put
    // the dashboard into a state the backend will reject.
    const timeframes = new Set(['M5', 'M15', 'H1', 'H4', 'D1']);
    const windows = new Set(['1H', '4H', '1D']);
    return {
      timeframe: timeframes.has(String(parsed.timeframe))
        ? String(parsed.timeframe)
        : DEFAULT_PREFERENCES.timeframe,
      symbol: typeof parsed.symbol === 'string' && /^[A-Z]{6}$/.test(parsed.symbol)
        ? parsed.symbol
        : null,
      strengthWindow: windows.has(String(parsed.strengthWindow))
        ? String(parsed.strengthWindow)
        : DEFAULT_PREFERENCES.strengthWindow,
      showInvalidated: parsed.showInvalidated === true
    };
  } catch {
    return { ...DEFAULT_PREFERENCES };
  }
}

export function savePreferences(prefs: Preferences): void {
  try {
    window.localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
  } catch {
    // Preferences are a convenience; losing them must never break the view.
  }
}
