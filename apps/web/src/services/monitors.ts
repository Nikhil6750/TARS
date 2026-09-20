/** Read-only monitor status for the HUD. Backend is the only source of truth. */
export interface MonitorStatus {
  mt5: { state: string; detail?: string };
  tradingview: { state: string; detail?: string };
  calendar: { state: string; detail?: string; replay?: boolean; next?: { event: string; currency: string; at: string } | null };
  news: { state: string; detail?: string };
  quote: { symbol: string; bid: number; ask: number; spread?: number | null; source: string } | null;
  replay: boolean;
}

export interface TarsAlert {
  title: string;
  summary: string;
  analysis?: string | null;
  replay: boolean;
  at: number;
}

export const ALERT_EVENT = 'tars-alert';

export async function fetchMonitorStatus(base = 'http://127.0.0.1:8000'): Promise<MonitorStatus | null> {
  try {
    const response = await fetch(`${base}/api/v1/monitors/status`);
    return response.ok ? ((await response.json()) as MonitorStatus) : null;
  } catch {
    return null;
  }
}

export function publishAlert(alert: TarsAlert): void {
  if (typeof window !== 'undefined') window.dispatchEvent(new CustomEvent<TarsAlert>(ALERT_EVENT, { detail: alert }));
}
