import React, { useState } from 'react';
import {
  Bell,
  Search,
  FileJson,
  X,
  Copy,
  Check
} from 'lucide-react';
import { TARSTradingEvent } from '../../types/trading-event';

interface AlertHistoryViewProps {
  alerts: TARSTradingEvent[];
  selectedAlert?: TARSTradingEvent | null;
  onSelectAlert: (alert: TARSTradingEvent | null) => void;
  onClearHistory?: () => void;
}

export const AlertHistoryView: React.FC<AlertHistoryViewProps> = ({
  alerts,
  selectedAlert,
  onSelectAlert,
  onClearHistory
}) => {
  const [search, setSearch] = useState('');
  const [copied, setCopied] = useState(false);

  const filteredAlerts = alerts.filter((a) => {
    if (!search) return true;
    const q = search.toLowerCase();
    return (
      a.symbol.toLowerCase().includes(q) ||
      a.state.toLowerCase().includes(q) ||
      a.strategy_id?.toLowerCase().includes(q) ||
      a.validation_status.toLowerCase().includes(q)
    );
  });

  const handleCopyJson = () => {
    if (selectedAlert) {
      navigator.clipboard.writeText(JSON.stringify(selectedAlert, null, 2));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  return (
    <div className="w-full h-full flex flex-col md:flex-row gap-4 p-3 md:p-6 overflow-hidden max-w-7xl mx-auto">
      {/* Alerts Table / Feed */}
      <div className="flex-1 flex flex-col glass-panel p-4 overflow-hidden bg-[#070d18]/90">
        {/* Header */}
        <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-2 pb-3 border-b border-slate-800">
          <div>
            <h1 className="text-base font-display-title font-bold text-slate-100 flex items-center gap-2">
              <Bell className="w-4 h-4 text-cyan-400" />
              ALERT & EVENT HISTORY
            </h1>
            <p className="text-[11px] font-mono text-slate-400">
              Deterministic chronological audit trail of all trading and system events.
            </p>
          </div>

          <div className="flex items-center gap-2 w-full sm:w-auto">
            <div className="relative flex-1 sm:w-48">
              <Search className="w-3.5 h-3.5 text-slate-500 absolute left-2.5 top-2.5" />
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Filter event log..."
                className="w-full bg-[#040810] border border-slate-700/80 rounded-lg pl-8 pr-2.5 py-1 text-xs font-mono text-slate-200 outline-none focus:border-cyan-500"
              />
            </div>
            {onClearHistory && (
              <button
                onClick={onClearHistory}
                className="px-2.5 py-1 bg-slate-800 hover:bg-slate-700 text-slate-400 hover:text-slate-200 rounded text-xs font-mono transition-colors"
              >
                Clear
              </button>
            )}
          </div>
        </div>

        {/* List of Alerts */}
        <div className="flex-1 overflow-y-auto mt-2 space-y-1.5 pr-1">
          {filteredAlerts.length === 0 ? (
            <div className="py-16 text-center text-slate-500 text-xs font-mono">
              NO TRADING EVENTS LOGGED
            </div>
          ) : (
            filteredAlerts.map((alert) => {
              const isSelected = selectedAlert?.event_id === alert.event_id;
              const isRisk = alert.state === 'RISK_WARNING' || alert.state === 'SYSTEM_WARNING';
              const isValid = alert.state === 'SETUP_VALID';

              return (
                <div
                  key={alert.event_id}
                  onClick={() => onSelectAlert(alert)}
                  className={`p-2.5 rounded-lg border transition-all cursor-pointer flex items-center justify-between gap-3 text-xs font-mono ${
                    isSelected
                      ? 'bg-cyan-500/20 border-cyan-500 shadow-[0_0_12px_rgba(0,240,255,0.2)]'
                      : 'bg-[#050912] border-slate-800/80 hover:border-slate-700 hover:bg-slate-900/60'
                  }`}
                >
                  <div className="flex items-center gap-3 min-w-0">
                    <span
                      className={`w-2 h-2 rounded-full shrink-0 ${
                        isValid ? 'bg-emerald-400' : isRisk ? 'bg-ruby-400 animate-ping' : 'bg-cyan-400'
                      }`}
                    />
                    <div className="truncate">
                      <div className="flex items-center gap-2">
                        <span className="font-bold text-slate-100">{alert.symbol}</span>
                        <span className="text-slate-400 text-[11px]">{alert.state}</span>
                        {alert.direction && alert.direction !== 'NONE' && (
                          <span className={`px-1 rounded text-[10px] ${alert.direction === 'LONG' ? 'text-emerald-400 bg-emerald-950/60' : 'text-ruby-400 bg-ruby-950/60'}`}>
                            {alert.direction}
                          </span>
                        )}
                      </div>
                      <div className="text-[10px] text-slate-500 truncate">
                        {alert.strategy_id || 'System'} • Status: {alert.validation_status}
                      </div>
                    </div>
                  </div>

                  <div className="text-right shrink-0 text-[10px] text-slate-500">
                    <div>{new Date(alert.timestamp).toLocaleTimeString()}</div>
                    <div className="text-cyan-400/80 font-numeric">{alert.source}</div>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>

      {/* Side Inspector Drawer */}
      {selectedAlert && (
        <div className="w-full md:w-96 glass-panel p-4 flex flex-col justify-between bg-[#081222]/95 border-cyan-500/30 overflow-y-auto">
          <div>
            <div className="flex items-center justify-between pb-2 border-b border-slate-800">
              <div className="flex items-center gap-2 text-xs font-mono font-bold text-cyan-300">
                <FileJson className="w-4 h-4" />
                <span>CANONICAL EVENT INSPECTOR</span>
              </div>
              <button
                onClick={() => onSelectAlert(null)}
                className="p-1 text-slate-400 hover:text-slate-100 rounded"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="mt-3 space-y-2 text-xs font-mono">
              <div className="p-2 rounded bg-slate-900/80 border border-slate-800">
                <div className="text-slate-500 text-[10px]">EVENT ID</div>
                <div className="text-slate-200 text-[11px] truncate">{selectedAlert.event_id}</div>
              </div>

              <div className="grid grid-cols-2 gap-2">
                <div className="p-2 rounded bg-slate-900/80 border border-slate-800">
                  <div className="text-slate-500 text-[10px]">SCHEMA VERSION</div>
                  <div className="text-cyan-400">{selectedAlert.schema_version}</div>
                </div>
                <div className="p-2 rounded bg-slate-900/80 border border-slate-800">
                  <div className="text-slate-500 text-[10px]">SOURCE</div>
                  <div className="text-slate-200">{selectedAlert.source}</div>
                </div>
              </div>

              {/* Raw JSON viewer */}
              <div className="mt-2">
                <div className="flex items-center justify-between text-[10px] text-slate-500 pb-1">
                  <span>RAW CONTRACT PAYLOAD</span>
                  <button
                    onClick={handleCopyJson}
                    className="flex items-center gap-1 text-cyan-400 hover:underline cursor-pointer"
                  >
                    {copied ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
                    <span>{copied ? 'Copied' : 'Copy JSON'}</span>
                  </button>
                </div>
                <pre className="p-2.5 rounded bg-[#03060c] border border-cyan-500/20 text-[10px] text-cyan-300 font-mono overflow-x-auto max-h-64">
                  {JSON.stringify(selectedAlert, null, 2)}
                </pre>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
