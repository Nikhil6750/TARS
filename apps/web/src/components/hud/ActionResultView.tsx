import React from 'react';
import {
  CheckCircle2,
  XCircle,
  AlertOctagon,
  Clock,
  Ban,
  Loader2,
} from 'lucide-react';
import { ActionResult, RiskLevel } from '../../types/actions';

interface ActionResultViewProps {
  result: ActionResult;
  onDismiss?: () => void;
}

export const ActionResultView: React.FC<ActionResultViewProps> = ({ result, onDismiss }) => {
  const isSuccess = result.status === 'SUCCEEDED';
  const isFailed = result.status === 'FAILED';
  const isDenied = result.status === 'DENIED';
  const isBlocked = result.status === 'BLOCKED';

  const getStatusBadge = () => {
    switch (result.status) {
      case 'SUCCEEDED':
        return (
          <span className="px-2 py-0.5 rounded text-[11px] font-mono font-bold bg-emerald-950/80 border border-emerald-500/50 text-emerald-300 flex items-center gap-1">
            <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
            SUCCEEDED
          </span>
        );
      case 'FAILED':
        return (
          <span className="px-2 py-0.5 rounded text-[11px] font-mono font-bold bg-rose-950/80 border border-rose-500/50 text-rose-300 flex items-center gap-1">
            <XCircle className="w-3.5 h-3.5 text-rose-400" />
            FAILED
          </span>
        );
      case 'DENIED':
        return (
          <span className="px-2 py-0.5 rounded text-[11px] font-mono font-bold bg-amber-950/80 border border-amber-500/50 text-amber-300 flex items-center gap-1">
            <Ban className="w-3.5 h-3.5 text-amber-400" />
            DENIED
          </span>
        );
      case 'BLOCKED':
        return (
          <span className="px-2 py-0.5 rounded text-[11px] font-mono font-bold bg-rose-950/90 border border-rose-600 text-rose-200 flex items-center gap-1 animate-pulse">
            <AlertOctagon className="w-3.5 h-3.5 text-rose-400" />
            BLOCKED
          </span>
        );
      case 'RUNNING':
      case 'PENDING':
        return (
          <span className="px-2 py-0.5 rounded text-[11px] font-mono font-bold bg-cyan-950/80 border border-cyan-500/50 text-cyan-300 flex items-center gap-1">
            <Loader2 className="w-3.5 h-3.5 text-cyan-400 animate-spin" />
            RUNNING
          </span>
        );
      default:
        return (
          <span className="px-2 py-0.5 rounded text-[11px] font-mono font-bold bg-slate-800 text-slate-300">
            {result.status}
          </span>
        );
    }
  };

  const getRiskBadge = (risk?: RiskLevel | null) => {
    if (!risk) return null;
    switch (risk) {
      case 'READ_ONLY':
        return <span className="px-1.5 py-0.5 rounded text-[10px] font-mono bg-blue-950/60 border border-blue-500/30 text-blue-300">READ_ONLY</span>;
      case 'LOW_RISK':
        return <span className="px-1.5 py-0.5 rounded text-[10px] font-mono bg-cyan-950/60 border border-cyan-500/30 text-cyan-300">LOW_RISK</span>;
      case 'CONFIRM_REQUIRED':
        return <span className="px-1.5 py-0.5 rounded text-[10px] font-mono bg-amber-950/60 border border-amber-500/30 text-amber-300">CONFIRM_REQUIRED</span>;
      case 'BLOCKED':
        return <span className="px-1.5 py-0.5 rounded text-[10px] font-mono bg-rose-950/80 border border-rose-500/50 text-rose-300">BLOCKED</span>;
    }
  };

  return (
    <div
      className={`rounded-xl p-3.5 font-mono select-none transition-all ${
        isSuccess
          ? 'bg-[#061412] border border-emerald-500/30 shadow-[0_0_15px_rgba(16,185,129,0.1)]'
          : isFailed || isBlocked
          ? 'bg-[#18090d] border border-rose-500/40 shadow-[0_0_15px_rgba(244,63,94,0.15)]'
          : isDenied
          ? 'bg-[#171208] border border-amber-500/30'
          : 'bg-[#09111e] border border-cyan-500/20'
      }`}
    >
      {/* Top Header */}
      <div className="flex items-center justify-between pb-2 border-b border-slate-800/80">
        <div className="flex items-center gap-2">
          {getStatusBadge()}
          {getRiskBadge(result.risk_level)}
        </div>

        <span className="text-[10px] text-slate-500">
          ID: {result.request_id.substring(0, 8)}
        </span>
      </div>

      {/* Summary */}
      <div className="mt-2.5">
        <p className="text-xs font-semibold text-slate-200 leading-relaxed">
          {result.summary}
        </p>
      </div>

      {/* Error Details if any */}
      {result.error && (
        <div className="mt-2 p-2 bg-rose-950/50 border border-rose-500/30 rounded text-[11px] text-rose-300">
          <span className="font-bold block text-rose-400 uppercase text-[9px] mb-0.5">Error:</span>
          {result.error}
        </div>
      )}

      {/* Structured Payload / Output Data */}
      {result.data && Object.keys(result.data).length > 0 && (
        <div className="mt-2.5 p-2 bg-black/40 border border-slate-800 rounded text-[10px] text-slate-400 overflow-x-auto">
          <span className="text-slate-500 block uppercase text-[9px] mb-1 font-bold">Result Data:</span>
          <pre className="text-cyan-300 whitespace-pre-wrap font-mono">
            {JSON.stringify(result.data, null, 2)}
          </pre>
        </div>
      )}

      {/* Timestamps & Dismiss */}
      <div className="mt-2.5 pt-2 border-t border-slate-800/60 flex items-center justify-between text-[10px] text-slate-500">
        <div className="flex items-center gap-1">
          <Clock className="w-3 h-3" />
          <span>{new Date(result.started_at).toLocaleTimeString()}</span>
          {result.completed_at && (
            <span>→ {new Date(result.completed_at).toLocaleTimeString()}</span>
          )}
        </div>

        {onDismiss && (
          <button
            onClick={onDismiss}
            className="text-slate-400 hover:text-slate-200 transition-colors underline text-[10px]"
          >
            Dismiss
          </button>
        )}
      </div>
    </div>
  );
};
