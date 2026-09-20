import React from 'react';
import { MonitorStatus, TarsAlert } from '../../services/monitors';

const DOT: Record<string, string> = {
  CONNECTED: 'bg-emerald-400', MONITORING: 'bg-emerald-400',
  ERROR: 'bg-rose-500', 'NOT INSTALLED': 'bg-slate-600', 'NOT FOUND': 'bg-amber-400',
  DISCONNECTED: 'bg-amber-400', 'NOT CONFIGURED': 'bg-slate-600', DISABLED: 'bg-slate-600',
};

const Indicator: React.FC<{ label: string; state?: string; detail?: string }> = ({ label, state, detail }) => (
  <span className="flex items-center gap-1" title={`${label}: ${state ?? 'UNKNOWN'}${detail ? ` - ${detail}` : ''}`}>
    <span className={`w-1.5 h-1.5 rounded-full ${DOT[state ?? ''] ?? 'bg-slate-600'}`} />
    <span className="text-[10px] tracking-wide text-slate-400">{label}</span>
  </span>
);

/** Truthful source indicators, live quote, and the latest alert. No fabricated badges. */
export const MonitorStrip: React.FC<{ status: MonitorStatus | null; alert: TarsAlert | null }> = ({ status, alert }) => {
  const quote = status?.quote;
  return (
    <div className="mt-2 shrink-0 space-y-1.5" data-testid="monitor-strip">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] text-slate-300 tabular-nums">
          {quote ? `${quote.symbol}  ${quote.bid.toFixed(5)}` : 'No live quote'}
          {quote && quote.source !== 'MT5' && (
            <span className="ml-1 text-[9px] text-amber-300 border border-amber-500/40 rounded px-1">{quote.source}</span>
          )}
        </span>
        <span className="flex items-center gap-2">
          <Indicator label="MT5" state={status?.mt5.state} detail={status?.mt5.detail} />
          <Indicator label="TV" state={status?.tradingview.state} detail={status?.tradingview.detail} />
          <Indicator label="CAL" state={status?.calendar.state} detail={status?.calendar.detail} />
          <Indicator label="NEWS" state={status?.news.state} detail={status?.news.detail} />
        </span>
      </div>
      {alert && (
        <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-1 text-[11px] leading-snug text-amber-100">
          <div className="font-medium">{alert.title}</div>
          <div className="text-amber-200/80">{alert.analysis || alert.summary}</div>
        </div>
      )}
    </div>
  );
};
