import React from 'react';
import {
  TrendingUp,
  AlertCircle,
  ShieldAlert,
  Volume2,
  CheckCircle,
  Activity,
  Layers,
  Sparkles,
} from 'lucide-react';

export interface ChartAnalysisData {
  instrument?: string | null;
  timeframe?: string | null;
  market_context: string;
  key_levels?: string[];
  possible_setup?: string | null;
  invalidation?: string | null;
  risk_notes?: string;
  provider?: string;
  disclaimer?: string;
  speech_text?: string;
}

interface ChartAnalysisCardProps {
  analysis: ChartAnalysisData;
  onDismiss?: () => void;
  onReplayAudio?: () => void;
  isSpeaking?: boolean;
}

export const ChartAnalysisCard: React.FC<ChartAnalysisCardProps> = ({
  analysis,
  onDismiss,
  onReplayAudio,
  isSpeaking = false,
}) => {
  const {
    instrument,
    timeframe,
    market_context,
    key_levels = [],
    possible_setup,
    invalidation,
    risk_notes,
    provider = 'Claude',
    disclaimer,
  } = analysis;

  const headerLabel = [instrument, timeframe].filter(Boolean).join(' · ') || 'Active Chart Analysis';

  return (
    <div className="bg-[#040812]/95 border border-cyan-500/40 rounded-xl p-3.5 shadow-[0_4px_24px_rgba(0,240,255,0.12)] backdrop-blur-md text-slate-200 animate-in fade-in slide-in-from-bottom-2 duration-300">
      {/* Header */}
      <div className="flex items-center justify-between pb-2 border-b border-cyan-500/20 mb-2.5">
        <div className="flex items-center gap-2">
          <div className="p-1 rounded bg-cyan-500/20 text-cyan-300">
            <Activity className="w-3.5 h-3.5" />
          </div>
          <div>
            <h4 className="text-xs font-mono font-bold text-cyan-300 tracking-wide">
              {headerLabel}
            </h4>
            <span className="text-[9px] font-mono text-slate-400">
              Provider: <span className="text-cyan-400">{provider}</span> · Read-Only Context
            </span>
          </div>
        </div>

        <div className="flex items-center gap-1.5">
          {onReplayAudio && (
            <button
              type="button"
              onClick={onReplayAudio}
              className={`p-1.5 rounded text-xs transition-colors ${
                isSpeaking
                  ? 'bg-cyan-500 text-slate-950 animate-pulse'
                  : 'bg-slate-800/80 hover:bg-cyan-950 text-cyan-400 border border-cyan-500/30'
              }`}
              title="Speak / Replay Voice Read"
            >
              <Volume2 className="w-3 h-3" />
            </button>
          )}

          {onDismiss && (
            <button
              type="button"
              onClick={onDismiss}
              className="px-1.5 py-0.5 rounded text-[10px] font-mono text-slate-400 hover:text-slate-100 hover:bg-slate-800 transition-colors"
            >
              ✕
            </button>
          )}
        </div>
      </div>

      {/* Market Context */}
      <div className="mb-2.5">
        <div className="flex items-center gap-1 text-[10px] font-mono text-slate-400 uppercase tracking-wider mb-1">
          <TrendingUp className="w-3 h-3 text-cyan-400" />
          <span>Market Context</span>
        </div>
        <p className="text-xs text-slate-100 leading-relaxed font-sans bg-slate-950/60 p-2 rounded border border-slate-800/80">
          {market_context}
        </p>
      </div>

      {/* Key Levels (if identified) */}
      {key_levels && key_levels.length > 0 && (
        <div className="mb-2.5">
          <div className="flex items-center gap-1 text-[10px] font-mono text-slate-400 uppercase tracking-wider mb-1">
            <Layers className="w-3 h-3 text-blue-400" />
            <span>Key Levels</span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {key_levels.map((lvl, idx) => (
              <span
                key={idx}
                className="px-2 py-0.5 rounded bg-blue-950/50 border border-blue-500/40 text-[10px] font-mono text-blue-200"
              >
                {lvl}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Hedged Possible Setup & Invalidation */}
      {(possible_setup || invalidation) && (
        <div className="grid grid-cols-1 gap-2 mb-2.5">
          {possible_setup && (
            <div className="bg-emerald-950/30 border border-emerald-500/30 rounded p-2 text-xs">
              <div className="flex items-center gap-1 text-[10px] font-mono text-emerald-400 font-semibold mb-0.5">
                <Sparkles className="w-3 h-3" />
                <span>QUALITATIVE READ</span>
              </div>
              <p className="text-slate-200 text-[11px] leading-snug">{possible_setup}</p>
            </div>
          )}

          {invalidation && (
            <div className="bg-amber-950/30 border border-amber-500/30 rounded p-2 text-xs">
              <div className="flex items-center gap-1 text-[10px] font-mono text-amber-400 font-semibold mb-0.5">
                <AlertCircle className="w-3 h-3" />
                <span>INVALIDATION CONDITION</span>
              </div>
              <p className="text-slate-200 text-[11px] leading-snug">{invalidation}</p>
            </div>
          )}
        </div>
      )}

      {/* Risk Notes */}
      {risk_notes && (
        <div className="mb-2 px-2 py-1.5 bg-rose-950/20 border border-rose-500/30 rounded text-[10px] font-mono text-rose-300/90 flex items-start gap-1.5">
          <ShieldAlert className="w-3.5 h-3.5 text-rose-400 shrink-0 mt-0.5" />
          <span className="leading-tight">{risk_notes}</span>
        </div>
      )}

      {/* Non-negotiable Disclaimer */}
      <div className="text-[9px] font-mono text-slate-500 border-t border-slate-800/80 pt-1.5 leading-tight flex items-center gap-1">
        <CheckCircle className="w-2.5 h-2.5 text-slate-400 shrink-0" />
        <span>
          {disclaimer ||
            'Qualitative read from TARS assistant. Not a quant_brain signal. No confidence score.'}
        </span>
      </div>
    </div>
  );
};
