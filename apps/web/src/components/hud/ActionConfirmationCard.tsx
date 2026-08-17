import React, { useState } from 'react';
import { AlertTriangle, ShieldCheck, XCircle, Terminal, Cpu } from 'lucide-react';
import { ActionRequest } from '../../types/actions';

interface ActionConfirmationCardProps {
  request: ActionRequest;
  onConfirm: (requestId: string) => Promise<void> | void;
  onDeny: (requestId: string, reason?: string) => Promise<void> | void;
  isProcessing?: boolean;
}

export const ActionConfirmationCard: React.FC<ActionConfirmationCardProps> = ({
  request,
  onConfirm,
  onDeny,
  isProcessing = false,
}) => {
  const [denyReason, setDenyReason] = useState('');
  const [showDenyInput, setShowDenyInput] = useState(false);

  const commandArg = request.arguments.command ? String(request.arguments.command) : null;
  const appArg = request.arguments.target ? String(request.arguments.target) : null;
  const urlArg = request.arguments.url ? String(request.arguments.url) : null;

  return (
    <div className="bg-[#0c1424] border-2 border-amber-500/60 rounded-xl p-4 shadow-[0_0_25px_rgba(245,158,11,0.2)] select-none font-sans animate-in fade-in duration-200">
      {/* Header with Risk Warning */}
      <div className="flex items-center justify-between pb-3 border-b border-amber-500/30">
        <div className="flex items-center gap-2">
          <div className="p-1.5 bg-amber-950/80 border border-amber-500/50 rounded-lg text-amber-400">
            <AlertTriangle className="w-5 h-5 animate-pulse" />
          </div>
          <div>
            <h4 className="text-sm font-bold tracking-wide text-amber-200 uppercase font-mono">
              Confirmation Required
            </h4>
            <span className="text-[11px] text-slate-400 font-mono">
              Action requires explicit user authorization
            </span>
          </div>
        </div>

        <div className="px-2.5 py-1 bg-amber-950/80 border border-amber-500/40 rounded text-[11px] font-mono font-bold text-amber-300">
          CONFIRM_REQUIRED
        </div>
      </div>

      {/* Target Details */}
      <div className="mt-3 space-y-2">
        <div className="flex items-center justify-between text-xs font-mono text-slate-300 bg-[#060b13] p-2 rounded border border-slate-800">
          <span className="text-slate-500">SKILL / VERB:</span>
          <span className="font-semibold text-cyan-400">
            {request.skill}.{request.action}()
          </span>
        </div>

        {/* Payload / Command Highlight */}
        {commandArg && (
          <div className="bg-[#03060a] p-2.5 rounded-lg border border-amber-500/30 font-mono text-xs">
            <div className="flex items-center gap-1.5 text-slate-500 text-[10px] uppercase mb-1">
              <Terminal className="w-3 h-3 text-amber-400" />
              <span>Target Terminal Command:</span>
            </div>
            <div className="text-emerald-400 font-bold break-all bg-black/40 p-2 rounded border border-slate-900">
              {commandArg}
            </div>
          </div>
        )}

        {appArg && (
          <div className="bg-[#03060a] p-2 rounded-lg border border-slate-800 font-mono text-xs flex justify-between">
            <span className="text-slate-500">TARGET APPLICATION:</span>
            <span className="text-cyan-300 font-semibold">{appArg}</span>
          </div>
        )}

        {urlArg && (
          <div className="bg-[#03060a] p-2 rounded-lg border border-slate-800 font-mono text-xs flex justify-between truncate">
            <span className="text-slate-500 shrink-0 mr-2">TARGET URL:</span>
            <span className="text-cyan-300 font-semibold truncate">{urlArg}</span>
          </div>
        )}

        {request.active_context && (
          <div className="text-[10px] font-mono text-slate-400 flex items-center gap-1">
            <Cpu className="w-3 h-3 text-slate-500" />
            <span>Active context: {request.active_context.executable} ({request.active_context.window_title || 'Untitled'})</span>
          </div>
        )}
      </div>

      {/* Denial Reason Input if active */}
      {showDenyInput && (
        <div className="mt-3">
          <input
            type="text"
            placeholder="Optional reason for denying..."
            value={denyReason}
            onChange={(e) => setDenyReason(e.target.value)}
            className="w-full px-3 py-1.5 bg-[#060b13] border border-rose-500/40 rounded text-xs font-mono text-slate-200 focus:outline-none focus:border-rose-400"
          />
        </div>
      )}

      {/* Action Buttons */}
      <div className="mt-4 flex items-center gap-2">
        <button
          onClick={() => onConfirm(request.id)}
          disabled={isProcessing}
          className="flex-1 flex items-center justify-center gap-2 py-2.5 px-4 bg-emerald-600 hover:bg-emerald-500 active:bg-emerald-700 text-slate-950 font-mono font-bold text-xs rounded-lg transition-all shadow-[0_0_15px_rgba(16,185,129,0.3)] disabled:opacity-50"
        >
          <ShieldCheck className="w-4 h-4" />
          <span>{isProcessing ? 'EXECUTING...' : 'CONFIRM & EXECUTE'}</span>
        </button>

        <button
          onClick={() => {
            if (!showDenyInput) {
              setShowDenyInput(true);
            } else {
              onDeny(request.id, denyReason || undefined);
            }
          }}
          disabled={isProcessing}
          className="flex items-center justify-center gap-1.5 py-2.5 px-4 bg-rose-950/60 hover:bg-rose-900/80 text-rose-300 border border-rose-500/40 font-mono text-xs font-semibold rounded-lg transition-all disabled:opacity-50"
        >
          <XCircle className="w-4 h-4" />
          <span>{showDenyInput ? 'SUBMIT DENIAL' : 'DENY'}</span>
        </button>
      </div>
    </div>
  );
};
