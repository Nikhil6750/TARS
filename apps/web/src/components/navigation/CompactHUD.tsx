import React from 'react';
import {
  Maximize2,
  Mic,
  AlertTriangle
} from 'lucide-react';
import { TARSCharacter } from '../character/TARSCharacter';
import { CompanionVisualState } from '../../types/companion';
import { TARSTradingEvent } from '../../types/trading-event';

interface CompactHUDProps {
  companionState: CompanionVisualState;
  onExpand: () => void;
  activeSetups: TARSTradingEvent[];
  criticalWarnings: string[];
  isListening: boolean;
  onTogglePushToTalk: () => void;
  audioVolume: number;
}

export const CompactHUD: React.FC<CompactHUDProps> = ({
  companionState,
  onExpand,
  activeSetups,
  criticalWarnings,
  isListening,
  onTogglePushToTalk,
  audioVolume
}) => {
  const validSetups = activeSetups.filter((s) => s.state === 'SETUP_VALID');
  const latestSetup = validSetups[0] || activeSetups[0];

  return (
    <div className="w-full h-full flex flex-col justify-between p-3 bg-[#05080e] border border-cyan-500/30 rounded-xl select-none font-sans overflow-hidden">
      {/* Top Bar */}
      <div className="flex items-center justify-between pb-2 border-b border-slate-800">
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-cyan-400 animate-ping" />
          <span className="font-display-title font-bold text-xs tracking-wider text-slate-100">
            TARS HUD
          </span>
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={onExpand}
            className="p-1 rounded bg-slate-900 hover:bg-slate-800 text-slate-400 hover:text-cyan-400 border border-slate-700 transition-colors"
            title="Expand to Full Workstation"
          >
            <Maximize2 className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {/* Center: Character Visualizer */}
      <div className="flex flex-col items-center justify-center my-2">
        <TARSCharacter
          state={companionState}
          audioVolume={audioVolume}
          size="compact"
          onClick={onTogglePushToTalk}
        />
      </div>

      {/* Active Setup Spotlight or Idle Status */}
      <div className="bg-[#09111e] rounded-lg p-2.5 border border-cyan-500/20">
        {latestSetup ? (
          <div>
            <div className="flex items-center justify-between text-[11px] font-mono">
              <span className="font-bold text-slate-200">{latestSetup.symbol}</span>
              <span
                className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${
                  latestSetup.direction === 'LONG'
                    ? 'bg-emerald-950 text-emerald-300 border border-emerald-500/30'
                    : latestSetup.direction === 'SHORT'
                    ? 'bg-ruby-950 text-ruby-300 border border-ruby-500/30'
                    : 'bg-slate-800 text-slate-300'
                }`}
              >
                {latestSetup.direction || 'NONE'}
              </span>
            </div>
            <div className="grid grid-cols-3 gap-1 mt-1.5 text-[10px] font-mono text-slate-300">
              <div>
                <span className="text-slate-500 block">ENTRY</span>
                <span>{latestSetup.entry ?? '—'}</span>
              </div>
              <div>
                <span className="text-slate-500 block">SL</span>
                <span className="text-rose-400">{latestSetup.stop_loss ?? '—'}</span>
              </div>
              <div>
                <span className="text-slate-500 block">TP (R:R)</span>
                <span className="text-emerald-400">
                  {latestSetup.take_profit ?? '—'} ({latestSetup.risk_reward ? `${latestSetup.risk_reward}R` : '—'})
                </span>
              </div>
            </div>
          </div>
        ) : (
          <div className="text-center py-1 text-slate-500 text-xs font-mono">
            ALL INSTRUMENTS IDLE
          </div>
        )}
      </div>

      {/* Warnings Bar */}
      {criticalWarnings.length > 0 && (
        <div className="mt-2 flex items-center gap-1.5 px-2 py-1 bg-amber-950/40 border border-amber-500/30 rounded text-amber-300 text-[10px] font-mono truncate">
          <AlertTriangle className="w-3 h-3 shrink-0 text-amber-400" />
          <span className="truncate">{criticalWarnings[0]}</span>
        </div>
      )}

      {/* Bottom Push To Talk Trigger */}
      <div className="mt-2 pt-2 border-t border-slate-800/80 flex items-center justify-between">
        <button
          onClick={onTogglePushToTalk}
          className={`flex-1 flex items-center justify-center gap-2 py-2 rounded-lg font-mono text-xs font-semibold transition-all ${
            isListening
              ? 'bg-emerald-500 text-slate-950 shadow-[0_0_15px_rgba(0,255,102,0.5)] animate-pulse'
              : 'bg-cyan-500/20 hover:bg-cyan-500/30 text-cyan-300 border border-cyan-500/40'
          }`}
        >
          <Mic className="w-3.5 h-3.5" />
          <span>{isListening ? 'LISTENING (RELEASE)' : 'HOLD TO TALK'}</span>
        </button>
      </div>
    </div>
  );
};
