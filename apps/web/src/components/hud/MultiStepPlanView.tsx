import React from 'react';
import { Play, CheckCircle2, XCircle, Clock, AlertTriangle, X, Loader2 } from 'lucide-react';
import { MultiStepActionPlan, ActionPlanStep, RiskLevel } from '../../types/actions';

interface MultiStepPlanViewProps {
  plan: MultiStepActionPlan;
  onExecute: () => Promise<void> | void;
  onResume: (approved: boolean) => Promise<void> | void;
  onCancel: () => void;
  isExecuting?: boolean;
}

export const MultiStepPlanView: React.FC<MultiStepPlanViewProps> = ({
  plan,
  onExecute,
  onResume,
  onCancel,
  isExecuting = false,
}) => {
  const getStepIcon = (status: ActionPlanStep['status']) => {
    switch (status) {
      case 'SUCCEEDED':
        return <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400 shrink-0" />;
      case 'RUNNING':
        return <Loader2 className="w-3.5 h-3.5 text-cyan-400 animate-spin shrink-0" />;
      case 'FAILED':
        return <XCircle className="w-3.5 h-3.5 text-rose-400 shrink-0" />;
      default:
        return <Clock className="w-3.5 h-3.5 text-slate-500 shrink-0" />;
    }
  };

  const getRiskBadge = (risk: RiskLevel) => {
    switch (risk) {
      case 'CONFIRM_REQUIRED':
        return <span className="px-1 py-0.2 rounded text-[9px] font-mono bg-amber-950/80 border border-amber-500/40 text-amber-300">CONFIRM</span>;
      case 'READ_ONLY':
        return <span className="px-1 py-0.2 rounded text-[9px] font-mono bg-blue-950/60 border border-blue-500/30 text-blue-300">READ</span>;
      case 'LOW_RISK':
        return <span className="px-1 py-0.2 rounded text-[9px] font-mono bg-cyan-950/60 border border-cyan-500/30 text-cyan-300">LOW</span>;
      case 'BLOCKED':
        return <span className="px-1 py-0.2 rounded text-[9px] font-mono bg-rose-950 border border-rose-600 text-rose-300">BLOCKED</span>;
    }
  };

  const isAwaiting = plan.status === 'AWAITING_CONFIRMATION';
  const isCompleted = plan.status === 'COMPLETED';

  return (
    <div className="bg-[#070e1b] border border-cyan-500/40 rounded-xl p-3 shadow-[0_0_20px_rgba(6,182,212,0.15)] font-mono text-xs select-none animate-in fade-in duration-150">
      {/* Header */}
      <div className="flex items-center justify-between pb-2 border-b border-slate-800">
        <div className="flex items-center gap-1.5 truncate">
          <span className="w-2 h-2 rounded-full bg-cyan-400 animate-pulse" />
          <span className="font-bold text-slate-100 truncate text-[11px]">PLAN: {plan.goal}</span>
        </div>

        <button
          onClick={onCancel}
          className="p-1 text-slate-500 hover:text-slate-200 transition-colors shrink-0 ml-1"
          title="Dismiss Plan"
        >
          <X className="w-3.5 h-3.5" />
        </button>
      </div>

      {/* Steps List */}
      <div className="my-2 space-y-1.5 max-h-36 overflow-y-auto custom-scrollbar pr-1">
        {plan.steps.map((step, idx) => {
          const isCurrent = idx === plan.current_step_index && plan.status === 'EXECUTING';
          return (
            <div
              key={idx}
              className={`p-1.5 rounded border text-[10px] transition-all flex items-center justify-between gap-1.5 ${
                isCurrent
                  ? 'bg-cyan-950/60 border-cyan-500/50 shadow-[0_0_10px_rgba(6,182,212,0.2)]'
                  : step.status === 'SUCCEEDED'
                  ? 'bg-emerald-950/20 border-emerald-500/20 text-slate-300'
                  : 'bg-black/40 border-slate-800/80 text-slate-400'
              }`}
            >
              <div className="flex items-center gap-1.5 truncate flex-1 min-w-0">
                {getStepIcon(step.status)}
                <span className="text-slate-500 shrink-0">#{step.step_number}</span>
                <span className="font-semibold text-cyan-300 shrink-0">{step.skill}.{step.action}</span>
                <span className="text-slate-300 truncate">{step.description}</span>
              </div>
              <div className="shrink-0">{getRiskBadge(step.risk_level)}</div>
            </div>
          );
        })}
      </div>

      {/* Confirmation Prompt if awaiting */}
      {isAwaiting && (
        <div className="p-2 my-2 bg-amber-950/60 border border-amber-500/40 rounded flex items-center justify-between gap-2">
          <div className="flex items-center gap-1 text-[10px] text-amber-300 truncate">
            <AlertTriangle className="w-3.5 h-3.5 text-amber-400 shrink-0" />
            <span className="truncate">Step requires authorization</span>
          </div>
          <div className="flex items-center gap-1 shrink-0">
            <button
              onClick={() => onResume(true)}
              className="px-2 py-0.5 bg-emerald-600 hover:bg-emerald-500 text-slate-950 font-bold rounded text-[10px]"
            >
              Authorize
            </button>
            <button
              onClick={() => onResume(false)}
              className="px-2 py-0.5 bg-rose-900/80 hover:bg-rose-800 text-rose-200 rounded text-[10px]"
            >
              Deny
            </button>
          </div>
        </div>
      )}

      {/* Plan Action Trigger */}
      {plan.status === 'PLANNING' && (
        <button
          onClick={onExecute}
          disabled={isExecuting}
          className="w-full mt-2 py-1.5 px-3 bg-cyan-600 hover:bg-cyan-500 active:bg-cyan-700 text-slate-950 font-bold rounded-lg text-xs flex items-center justify-center gap-1.5 shadow-[0_0_15px_rgba(6,182,212,0.3)] disabled:opacity-50"
        >
          <Play className="w-3.5 h-3.5 fill-current" />
          <span>{isExecuting ? 'EXECUTING PLAN...' : `RUN ${plan.steps.length}-STEP PLAN`}</span>
        </button>
      )}

      {isCompleted && (
        <div className="mt-1 text-center text-emerald-400 text-[10px] font-bold">
          Plan successfully completed!
        </div>
      )}
    </div>
  );
};
