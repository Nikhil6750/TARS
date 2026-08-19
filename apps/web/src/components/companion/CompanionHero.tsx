import React, { useState } from 'react';
import {
  Mic,
  Send,
  Layers,
  ArrowUpRight,
  ArrowDownRight,
  ShieldAlert,
  Terminal
} from 'lucide-react';
import { TARSCharacter } from '../character/TARSCharacter';
import { CompanionVisualState, ConnectionStatus } from '../../types/companion';
import { TARSTradingEvent } from '../../types/trading-event';

interface CompanionHeroProps {
  companionState: CompanionVisualState;
  connectionStatus: ConnectionStatus;
  latencyMs: number;
  activeSetups: TARSTradingEvent[];
  criticalWarnings: string[];
  isListening: boolean;
  onTogglePushToTalk: () => void;
  audioVolume: number;
  onSendMessage: (text: string) => void;
  onInspectSetup: (setup: TARSTradingEvent) => void;
  /** Real streamed reply text, shown in place of the generic THINKING
   * placeholder as soon as the first delta arrives -- empty otherwise. */
  streamingAnswer?: string;
}

export const CompanionHero: React.FC<CompanionHeroProps> = ({
  companionState,
  latencyMs,
  activeSetups,
  criticalWarnings,
  isListening,
  onTogglePushToTalk,
  audioVolume,
  onSendMessage,
  onInspectSetup,
  streamingAnswer,
}) => {
  const [inputText, setInputText] = useState('');

  const validSetups = activeSetups.filter((s) => s.state === 'SETUP_VALID');
  const spotlightSetup = validSetups[0] || activeSetups[0];

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (inputText.trim()) {
      onSendMessage(inputText.trim());
      setInputText('');
    }
  };

  return (
    <div className="w-full h-full flex flex-col gap-4 p-3 md:p-6 overflow-y-auto max-w-7xl mx-auto">
      {/* Top Banner / Warnings ticker */}
      {criticalWarnings.length > 0 && (
        <div className="w-full bg-amber-950/40 border border-amber-500/40 rounded-xl p-3 flex items-center justify-between gap-3 shadow-[0_0_15px_rgba(255,183,0,0.15)] animate-pulse-subtle">
          <div className="flex items-center gap-2 text-amber-300 font-mono text-xs">
            <ShieldAlert className="w-4 h-4 text-amber-400 shrink-0" />
            <span className="font-semibold uppercase tracking-wider">SYSTEM ADVISORY:</span>
            <span className="text-slate-200">{criticalWarnings[0]}</span>
          </div>
          <span className="text-[10px] text-amber-400/80 font-numeric uppercase px-2 py-0.5 rounded bg-amber-900/40 border border-amber-500/20">
            ACTION REQUIRED
          </span>
        </div>
      )}

      {/* Main Grid: Visual Personality Hero + Spotlight Setup */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-4">
        {/* Left Col (7/12): TARS Visual Personality Matrix */}
        <div className="lg:col-span-7 glass-panel p-6 flex flex-col items-center justify-between relative overflow-hidden bg-gradient-to-b from-[#091322]/80 to-[#040811]/90">
          {/* Background Grid Accent */}
          <div className="absolute inset-0 bg-grid-pattern opacity-40 pointer-events-none" />

          {/* Header Row inside card */}
          <div className="w-full flex items-center justify-between text-xs font-mono text-slate-400 z-10 pb-4 border-b border-cyan-500/10">
            <div className="flex items-center gap-2">
              <Terminal className="w-3.5 h-3.5 text-cyan-400" />
              <span>TARS COMPANION INTERFACE</span>
            </div>
          </div>

          {/* Interactive Character Face */}
          <div className="my-6 z-10 flex flex-col items-center">
            <TARSCharacter
              state={companionState}
              audioVolume={audioVolume}
              size="hero"
              onClick={onTogglePushToTalk}
            />
            <div className="mt-3 text-center max-w-md">
              {companionState === 'THINKING' && streamingAnswer ? (
                <p className="text-xs font-mono text-cyan-200 whitespace-pre-wrap text-left">
                  {streamingAnswer}
                  <span className="inline-block w-1.5 h-3 bg-cyan-400 animate-pulse ml-0.5 align-middle" />
                </p>
              ) : (
                <p className="text-xs font-mono text-slate-400">
                  {companionState === 'IDLE' && 'Standing by. Monitoring quantitative market streams.'}
                  {companionState === 'WAKE' && 'Waking up... Listening for command.'}
                  {companionState === 'LISTENING' && 'Capturing voice input... (Release button when done)'}
                  {companionState === 'THINKING' && 'Waiting for the assistant...'}
                  {companionState === 'SPEAKING' && 'Transmitting response via localized voice synthesis.'}
                  {companionState === 'ALERT' && 'Setup trigger confirmed. Check active parameters.'}
                  {companionState === 'WARNING' && 'Account risk or data quality warning active.'}
                </p>
              )}
            </div>
          </div>

          {/* Quick Voice / Text Command Bar */}
          <div className="w-full z-10 mt-2">
            <form onSubmit={handleSubmit} className="flex items-center gap-2 bg-[#060b14]/90 p-1.5 rounded-xl border border-cyan-500/30 shadow-[0_0_12px_rgba(0,240,255,0.1)]">
              {/* Push to talk orb */}
              <button
                type="button"
                onClick={onTogglePushToTalk}
                title={isListening ? 'Release to Send' : 'Push to Talk'}
                className={`p-2.5 rounded-lg font-mono text-xs font-semibold flex items-center gap-1.5 transition-all cursor-pointer ${
                  isListening
                    ? 'bg-emerald-500 text-slate-950 shadow-[0_0_15px_rgba(0,255,102,0.6)] animate-pulse'
                    : 'bg-cyan-500/20 hover:bg-cyan-500/30 text-cyan-300 border border-cyan-500/40'
                }`}
              >
                <Mic className="w-4 h-4" />
                <span className="hidden sm:inline">{isListening ? 'LISTENING' : 'VOICE'}</span>
              </button>

              <input
                type="text"
                value={inputText}
                onChange={(e) => setInputText(e.target.value)}
                placeholder="Ask TARS ('Analyze Gold', 'Risk status', 'Active setups')..."
                className="flex-1 bg-transparent px-3 py-2 text-xs font-sans text-slate-100 placeholder-slate-500 outline-none"
              />

              <button
                type="submit"
                disabled={!inputText.trim()}
                className="p-2 rounded-lg bg-cyan-500 hover:bg-cyan-400 disabled:opacity-30 text-slate-950 font-bold transition-all cursor-pointer"
              >
                <Send className="w-4 h-4" />
              </button>
            </form>
          </div>
        </div>

        {/* Right Col (5/12): Active Setup Spotlight & Risk Summary */}
        <div className="lg:col-span-5 flex flex-col gap-4">
          {/* Spotlight Card */}
          <div className="glass-panel p-5 flex-1 flex flex-col justify-between bg-gradient-to-b from-[#0a1426]/90 to-[#060c18]/90">
            <div>
              <div className="flex items-center justify-between pb-3 border-b border-slate-800">
                <div className="flex items-center gap-2">
                  <Layers className="w-4 h-4 text-cyan-400" />
                  <span className="font-display-title font-bold text-xs tracking-wider text-slate-200">
                    SETUP SPOTLIGHT
                  </span>
                </div>
                {spotlightSetup && (
                  <span
                    className={`px-2 py-0.5 rounded text-[10px] font-mono font-bold uppercase ${
                      spotlightSetup.validation_status === 'VALID'
                        ? 'bg-emerald-950 text-emerald-300 border border-emerald-500/40'
                        : spotlightSetup.validation_status === 'INVALID'
                        ? 'bg-ruby-950 text-ruby-300 border border-ruby-500/40'
                        : 'bg-amber-950 text-amber-300 border border-amber-500/40'
                    }`}
                  >
                    {spotlightSetup.validation_status}
                  </span>
                )}
              </div>

              {spotlightSetup ? (
                <div className="mt-4 flex flex-col gap-3">
                  <div className="flex items-center justify-between">
                    <div>
                      <div className="text-xl font-mono font-bold text-slate-100 flex items-center gap-2">
                        {spotlightSetup.symbol}
                        <span className="text-xs text-slate-400 font-normal">
                          {spotlightSetup.strategy_id || 'manual_trigger'}
                        </span>
                      </div>
                      <div className="text-[11px] font-mono text-cyan-400">
                        {spotlightSetup.state}
                      </div>
                    </div>

                    <div
                      className={`flex items-center gap-1 px-3 py-1.5 rounded-lg font-mono font-bold text-sm ${
                        spotlightSetup.direction === 'LONG'
                          ? 'bg-emerald-950/80 text-emerald-300 border border-emerald-500/40'
                          : spotlightSetup.direction === 'SHORT'
                          ? 'bg-ruby-950/80 text-ruby-300 border border-ruby-500/40'
                          : 'bg-slate-800 text-slate-300'
                      }`}
                    >
                      {spotlightSetup.direction === 'LONG' ? (
                        <ArrowUpRight className="w-4 h-4" />
                      ) : spotlightSetup.direction === 'SHORT' ? (
                        <ArrowDownRight className="w-4 h-4" />
                      ) : null}
                      <span>{spotlightSetup.direction || 'NONE'}</span>
                    </div>
                  </div>

                  {/* Quantitative Parameters Grid */}
                  <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mt-2 p-3 rounded-lg bg-[#040810]/70 border border-slate-800 text-xs font-mono">
                    <div>
                      <span className="text-slate-500 text-[10px] block">ENTRY</span>
                      <span className="font-bold text-slate-200">{spotlightSetup.entry ?? '—'}</span>
                    </div>
                    <div>
                      <span className="text-slate-500 text-[10px] block">STOP LOSS</span>
                      <span className="font-bold text-rose-400">{spotlightSetup.stop_loss ?? '—'}</span>
                    </div>
                    <div>
                      <span className="text-slate-500 text-[10px] block">TARGET (TP)</span>
                      <span className="font-bold text-emerald-400">{spotlightSetup.take_profit ?? '—'}</span>
                    </div>
                    <div>
                      <span className="text-slate-500 text-[10px] block">R:R / RISK</span>
                      <span className="font-bold text-cyan-300">
                        {spotlightSetup.risk_reward ? `${spotlightSetup.risk_reward}R` : '—'}
                        {spotlightSetup.risk_percent ? ` (${spotlightSetup.risk_percent}%)` : ''}
                      </span>
                    </div>
                  </div>

                  {/* Reason Codes */}
                  {spotlightSetup.reason_codes && spotlightSetup.reason_codes.length > 0 && (
                    <div className="mt-1">
                      <span className="text-[10px] font-mono text-slate-500 uppercase block mb-1">
                        CONFIRMED CONFLUENCES / REASON CODES:
                      </span>
                      <div className="flex flex-wrap gap-1">
                        {spotlightSetup.reason_codes.map((code) => (
                          <span
                            key={code}
                            className="px-2 py-0.5 rounded bg-cyan-950/50 border border-cyan-500/20 text-[10px] font-mono text-cyan-300"
                          >
                            {code}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Warnings */}
                  {spotlightSetup.warnings && spotlightSetup.warnings.length > 0 && (
                    <div className="mt-1 flex items-start gap-1.5 p-2 bg-amber-950/30 border border-amber-500/20 rounded text-[11px] font-mono text-amber-300">
                      <span className="truncate">{spotlightSetup.warnings[0]}</span>
                    </div>
                  )}
                </div>
              ) : (
                <div className="py-12 text-center text-slate-500 text-xs font-mono">
                  NO ACTIVE SETUP SPOTLIGHT
                </div>
              )}
            </div>

            {spotlightSetup && (
              <button
                onClick={() => onInspectSetup(spotlightSetup)}
                className="mt-4 w-full py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-mono rounded-lg transition-colors cursor-pointer"
              >
                VIEW FULL QUANT SPECIFICATION →
              </button>
            )}
          </div>

          {/* Quick Metrics Bar */}
          <div className="glass-panel p-4 grid grid-cols-3 gap-2 text-center font-mono">
            <div>
              <span className="text-[10px] text-slate-500 block">ACTIVE SETUPS</span>
              <span className="text-lg font-bold text-cyan-400">{activeSetups.length}</span>
            </div>
            <div>
              <span className="text-[10px] text-slate-500 block">VALIDATED</span>
              <span className="text-lg font-bold text-emerald-400">{validSetups.length}</span>
            </div>
            <div>
              <span className="text-[10px] text-slate-500 block">LATENCY</span>
              <span className="text-lg font-bold text-slate-300">{latencyMs}ms</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
