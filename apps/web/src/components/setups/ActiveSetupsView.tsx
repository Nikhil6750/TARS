import React, { useState } from 'react';
import {
  Layers,
  Search,
  ArrowUpRight,
  ArrowDownRight,
  AlertTriangle
} from 'lucide-react';
import { TARSTradingEvent } from '../../types/trading-event';

interface ActiveSetupsViewProps {
  setups: TARSTradingEvent[];
  onSelectSetup: (setup: TARSTradingEvent) => void;
}

export const ActiveSetupsView: React.FC<ActiveSetupsViewProps> = ({ setups, onSelectSetup }) => {
  const [searchQuery, setSearchQuery] = useState('');
  const [directionFilter, setDirectionFilter] = useState<string>('ALL');
  const [statusFilter, setStatusFilter] = useState<string>('ALL');

  const filteredSetups = setups.filter((setup) => {
    if (searchQuery && !setup.symbol.toLowerCase().includes(searchQuery.toLowerCase()) && !setup.strategy_id?.toLowerCase().includes(searchQuery.toLowerCase())) {
      return false;
    }
    if (directionFilter !== 'ALL' && setup.direction !== directionFilter) {
      return false;
    }
    if (statusFilter !== 'ALL' && setup.validation_status !== statusFilter) {
      return false;
    }
    return true;
  });

  return (
    <div className="w-full h-full flex flex-col gap-4 p-3 md:p-6 overflow-y-auto max-w-7xl mx-auto">
      {/* Header & Filter Controls */}
      <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3 pb-3 border-b border-cyan-500/20">
        <div>
          <h1 className="text-lg font-display-title font-bold text-slate-100 flex items-center gap-2">
            <Layers className="w-5 h-5 text-cyan-400" />
            ACTIVE QUANTITATIVE SETUPS
          </h1>
          <p className="text-xs font-mono text-slate-400">
            Real-time deterministic trade setups. Zero execution, zero fabricated confidence metrics.
          </p>
        </div>

        {/* Filters */}
        <div className="flex flex-wrap items-center gap-2 w-full sm:w-auto">
          {/* Search */}
          <div className="relative flex-1 sm:w-44">
            <Search className="w-3.5 h-3.5 text-slate-500 absolute left-2.5 top-2.5" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search symbol..."
              className="w-full bg-[#08101e] border border-slate-700/80 rounded-lg pl-8 pr-2.5 py-1.5 text-xs font-mono text-slate-200 placeholder-slate-500 outline-none focus:border-cyan-500"
            />
          </div>

          {/* Direction Filter */}
          <select
            value={directionFilter}
            onChange={(e) => setDirectionFilter(e.target.value)}
            className="bg-[#08101e] border border-slate-700/80 rounded-lg px-2.5 py-1.5 text-xs font-mono text-slate-300 outline-none cursor-pointer"
          >
            <option value="ALL">All Directions</option>
            <option value="LONG">Long Only</option>
            <option value="SHORT">Short Only</option>
            <option value="NONE">None</option>
          </select>

          {/* Status Filter */}
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="bg-[#08101e] border border-slate-700/80 rounded-lg px-2.5 py-1.5 text-xs font-mono text-slate-300 outline-none cursor-pointer"
          >
            <option value="ALL">All Statuses</option>
            <option value="VALID">Valid</option>
            <option value="PENDING">Pending</option>
            <option value="INVALID">Invalid</option>
            <option value="EXPIRED">Expired</option>
          </select>
        </div>
      </div>

      {/* Setups Grid */}
      {filteredSetups.length === 0 ? (
        <div className="glass-panel p-12 text-center flex flex-col items-center justify-center">
          <Layers className="w-8 h-8 text-slate-600 mb-2" />
          <p className="text-sm font-mono text-slate-400">No active setups match the specified filters.</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {filteredSetups.map((setup) => {
            const isValid = setup.validation_status === 'VALID';
            const isInvalid = setup.validation_status === 'INVALID';

            return (
              <div
                key={setup.event_id}
                onClick={() => onSelectSetup(setup)}
                className="glass-panel p-4 flex flex-col justify-between hover:border-cyan-500/50 hover:shadow-[0_0_16px_rgba(0,240,255,0.15)] transition-all cursor-pointer bg-gradient-to-b from-[#0a1324]/90 to-[#060b14]/95"
              >
                <div>
                  {/* Top Row: Symbol + Status Badge */}
                  <div className="flex items-start justify-between">
                    <div>
                      <div className="flex items-center gap-2">
                        <span className="text-base font-mono font-bold text-slate-100">{setup.symbol}</span>
                        <span
                          className={`flex items-center gap-0.5 px-2 py-0.5 rounded text-[10px] font-mono font-bold ${
                            setup.direction === 'LONG'
                              ? 'bg-emerald-950 text-emerald-300 border border-emerald-500/30'
                              : setup.direction === 'SHORT'
                              ? 'bg-ruby-950 text-ruby-300 border border-ruby-500/30'
                              : 'bg-slate-800 text-slate-400'
                          }`}
                        >
                          {setup.direction === 'LONG' && <ArrowUpRight className="w-3 h-3" />}
                          {setup.direction === 'SHORT' && <ArrowDownRight className="w-3 h-3" />}
                          {setup.direction || 'NONE'}
                        </span>
                      </div>
                      <span className="text-[11px] font-mono text-slate-400 block truncate">
                        {setup.strategy_id || 'System Event'}
                      </span>
                    </div>

                    <span
                      className={`px-2 py-0.5 rounded text-[10px] font-mono font-bold uppercase ${
                        isValid
                          ? 'bg-emerald-950/80 text-emerald-300 border border-emerald-500/40'
                          : isInvalid
                          ? 'bg-ruby-950/80 text-ruby-300 border border-ruby-500/40'
                          : 'bg-amber-950/80 text-amber-300 border border-amber-500/40'
                      }`}
                    >
                      {setup.validation_status}
                    </span>
                  </div>

                  {/* Quantitative Parameters */}
                  <div className="grid grid-cols-2 gap-2 my-3 p-2.5 rounded-lg bg-[#040810] border border-slate-800 text-xs font-mono">
                    <div>
                      <span className="text-[10px] text-slate-500 block">ENTRY</span>
                      <span className="font-bold text-slate-200">{setup.entry ?? '—'}</span>
                    </div>
                    <div>
                      <span className="text-[10px] text-slate-500 block">STOP LOSS</span>
                      <span className="font-bold text-rose-400">{setup.stop_loss ?? '—'}</span>
                    </div>
                    <div>
                      <span className="text-[10px] text-slate-500 block">TARGET (TP)</span>
                      <span className="font-bold text-emerald-400">{setup.take_profit ?? '—'}</span>
                    </div>
                    <div>
                      <span className="text-[10px] text-slate-500 block">R:R (RISK %)</span>
                      <span className="font-bold text-cyan-300">
                        {setup.risk_reward ? `${setup.risk_reward}R` : '—'}
                        {setup.risk_percent ? ` (${setup.risk_percent}%)` : ''}
                      </span>
                    </div>
                  </div>

                  {/* Reason Codes */}
                  {setup.reason_codes && setup.reason_codes.length > 0 && (
                    <div className="flex flex-wrap gap-1 mb-2">
                      {setup.reason_codes.slice(0, 3).map((code) => (
                        <span
                          key={code}
                          className="px-1.5 py-0.5 rounded bg-cyan-950/40 border border-cyan-500/20 text-[9px] font-mono text-cyan-300"
                        >
                          {code}
                        </span>
                      ))}
                      {setup.reason_codes.length > 3 && (
                        <span className="text-[9px] font-mono text-slate-500">
                          +{setup.reason_codes.length - 3} more
                        </span>
                      )}
                    </div>
                  )}

                  {/* Warnings */}
                  {setup.warnings && setup.warnings.length > 0 && (
                    <div className="flex items-center gap-1.5 p-1.5 bg-amber-950/30 border border-amber-500/20 rounded text-[10px] font-mono text-amber-300 truncate">
                      <AlertTriangle className="w-3 h-3 text-amber-400 shrink-0" />
                      <span className="truncate">{setup.warnings[0]}</span>
                    </div>
                  )}
                </div>

                {/* Footer: Timestamp & Source */}
                <div className="mt-3 pt-2 border-t border-slate-800 flex items-center justify-between text-[10px] font-mono text-slate-500">
                  <span>SRC: {setup.source}</span>
                  <span>{new Date(setup.timestamp).toLocaleTimeString()}</span>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};
