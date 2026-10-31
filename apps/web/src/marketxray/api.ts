/**
 * Typed client for the Market X-Ray API.
 *
 * Every call reports failure as a thrown `ApiError` carrying the backend's own
 * message, so the UI can show what actually went wrong instead of a blank
 * panel. Nothing here fabricates a fallback value on error.
 */
import type {
  ChartPayload,
  Dashboard,
  EventsPayload,
  LeadersSnapshot,
  MarketStatus,
  OrderBlocksPayload,
  SetupStatus,
  StrengthSnapshot,
  XRaySnapshot
} from './types';
import { backendHttpUrl, backendWsUrl } from '../runtime/backendUrl';
import { isDesktop } from './desktop';

export const API_BASE = backendHttpUrl();

export const WS_URL = backendWsUrl('/api/v1/market/ws');

const ROOT = `${API_BASE}/api/v1/market`;

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly remedy?: string
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function get<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${ROOT}${path}`, init);
  } catch {
    throw new ApiError(
      `Cannot reach the TARS backend at ${API_BASE}.`,
      0,
      isDesktop() ? undefined : 'Start it with scripts\\start-market-xray.cmd'
    );
  }
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    let remedy: string | undefined;
    try {
      const body = await response.json();
      const detail = body?.detail;
      if (typeof detail === 'string') {
        message = detail;
      } else if (detail && typeof detail === 'object') {
        message = String(detail.message ?? message);
        remedy = detail.remedy;
      }
    } catch {
      // Keep the status line when the body is not JSON.
    }
    throw new ApiError(message, response.status, remedy);
  }
  return (await response.json()) as T;
}

function post<T>(path: string): Promise<T> {
  return get<T>(path, { method: 'POST' });
}

export const api = {
  dashboard: (timeframe: string, symbol?: string, strengthWindow?: string) => {
    const params = new URLSearchParams({ timeframe });
    if (symbol) params.set('symbol', symbol);
    if (strengthWindow) params.set('strength_window', strengthWindow);
    return get<Dashboard>(`/dashboard?${params}`);
  },
  health: () => get<{ status: MarketStatus; store: Record<string, unknown> }>('/health'),
  setup: (csvRoot?: string) =>
    get<SetupStatus>(`/setup${csvRoot ? `?csv_root=${encodeURIComponent(csvRoot)}` : ''}`),
  startImport: (csvRoot: string) =>
    post<SetupStatus['import']>(`/import?csv_root=${encodeURIComponent(csvRoot)}`),
  xray: (timeframe: string) => get<XRaySnapshot>(`/xray?timeframe=${timeframe}`),
  strength: (window: string) => get<StrengthSnapshot>(`/strength?window=${window}`),
  leaders: (timeframe: string) => get<LeadersSnapshot>(`/leaders?timeframe=${timeframe}`),
  candles: (symbol: string, timeframe: string, limit = 300) =>
    get<ChartPayload>(`/candles/${symbol}?timeframe=${timeframe}&limit=${limit}`),
  orderBlocks: (symbol: string, timeframe: string) =>
    get<OrderBlocksPayload>(`/order-blocks/${symbol}?timeframe=${timeframe}`),
  events: (limit = 60, symbol?: string) =>
    get<EventsPayload>(`/events?limit=${limit}${symbol ? `&symbol=${symbol}` : ''}`),

  // Replay control. The cursor lives on the backend, so every client sees the
  // same moment.
  seek: (iso: string) =>
    post<{ cursor: string; status: MarketStatus }>(`/replay/seek?to=${encodeURIComponent(iso)}`),
  step: (timeframe: string, bars: number) =>
    post<{ cursor: string; status: MarketStatus }>(
      `/replay/step?timeframe=${timeframe}&bars=${bars}`
    ),
  reset: () => post<{ cursor: string; status: MarketStatus }>('/replay/reset')
};
