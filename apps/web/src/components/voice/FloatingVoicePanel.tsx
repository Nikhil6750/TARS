import React, { useEffect, useRef } from 'react';
import {
  Mic,
  MicOff,
  Maximize2,
  Minimize2,
  X,
  Radio,
  Sparkles,
  AlertCircle,
  Square,
  Volume2,
} from 'lucide-react';
import { CompanionVisualState } from '../../types/companion';
import { WakeWordStatusInfo } from '../../services/wake-word';

interface FloatingVoicePanelProps {
  companionState: CompanionVisualState;
  isListening: boolean;
  onTogglePushToTalk: () => void;
  audioVolume: number;
  liveTranscript?: string;
  streamedText?: string;
  responseSpeakerText?: string;
  errorMessage?: string | null;
  wakeStatus?: WakeWordStatusInfo;
  onToggleWakeListening?: () => void;
  onInterruptSpeech?: () => void;
  onExpandToHUD?: () => void;
  onExpandToWorkstation?: () => void;
  onDismiss?: () => void;
  activeContextTitle?: string;
}

export const FloatingVoicePanel: React.FC<FloatingVoicePanelProps> = ({
  companionState,
  isListening,
  onTogglePushToTalk,
  audioVolume,
  liveTranscript,
  streamedText,
  responseSpeakerText,
  errorMessage,
  wakeStatus,
  onToggleWakeListening,
  onInterruptSpeech,
  onExpandToHUD,
  onExpandToWorkstation,
  onDismiss,
  activeContextTitle,
}) => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const animFrameRef = useRef<number | null>(null);
  const smoothedVolRef = useRef(0);

  // Real-time audio waveform visualizer (NO decorative orb)
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let phase = 0;

    const render = () => {
      const width = canvas.width;
      const height = canvas.height;
      ctx.clearRect(0, 0, width, height);

      // Smooth amplitude target
      const targetVol = isListening || companionState === 'SPEAKING' ? Math.max(0.08, audioVolume) : 0.04;
      smoothedVolRef.current += (targetVol - smoothedVolRef.current) * 0.25;
      const currentVol = smoothedVolRef.current;

      phase += (0.05 + currentVol * 0.15);

      const numBars = 36;
      const barSpacing = width / numBars;
      const centerY = height / 2;

      for (let i = 0; i < numBars; i++) {
        const x = i * barSpacing + barSpacing / 2;
        // Bell-curve envelope so edges taper smoothly
        const normX = (i / (numBars - 1)) * 2 - 1; // -1 to 1
        const envelope = Math.max(0, 1 - normX * normX);

        // Sine harmonic wave
        const wave1 = Math.sin(phase + i * 0.35);
        const wave2 = Math.cos(phase * 0.8 + i * 0.2);
        const combined = (wave1 * 0.6 + wave2 * 0.4);

        const barHeight = Math.max(4, envelope * currentVol * (height * 0.85) * (0.5 + Math.abs(combined) * 0.5));

        // State-dependent vibrant neon color gradient
        const gradient = ctx.createLinearGradient(0, centerY - barHeight / 2, 0, centerY + barHeight / 2);
        if (errorMessage || companionState === 'WARNING') {
          gradient.addColorStop(0, 'rgba(244, 63, 94, 0.9)');
          gradient.addColorStop(1, 'rgba(225, 29, 72, 0.4)');
        } else if (companionState === 'SPEAKING') {
          gradient.addColorStop(0, 'rgba(168, 85, 247, 0.95)');
          gradient.addColorStop(1, 'rgba(59, 130, 246, 0.6)');
        } else if (companionState === 'THINKING') {
          gradient.addColorStop(0, 'rgba(6, 182, 212, 0.95)');
          gradient.addColorStop(1, 'rgba(14, 116, 144, 0.5)');
        } else if (isListening || companionState === 'LISTENING') {
          gradient.addColorStop(0, 'rgba(16, 185, 129, 0.95)');
          gradient.addColorStop(1, 'rgba(5, 150, 105, 0.5)');
        } else {
          // Idle / Wake Ready
          gradient.addColorStop(0, 'rgba(56, 189, 248, 0.6)');
          gradient.addColorStop(1, 'rgba(30, 58, 138, 0.2)');
        }

        ctx.fillStyle = gradient;
        ctx.beginPath();
        const barW = Math.max(2.5, barSpacing * 0.55);
        const radius = barW / 2;
        ctx.roundRect(x - barW / 2, centerY - barHeight / 2, barW, barHeight, radius);
        ctx.fill();
      }

      animFrameRef.current = requestAnimationFrame(render);
    };

    render();

    return () => {
      if (animFrameRef.current !== null) {
        cancelAnimationFrame(animFrameRef.current);
      }
    };
  }, [companionState, isListening, audioVolume, errorMessage]);

  // Status Badge Label & Colors
  const getStatusBadge = () => {
    if (errorMessage) {
      return {
        label: 'ERROR',
        colorClass: 'bg-rose-950/80 border-rose-500/60 text-rose-300',
        dotClass: 'bg-rose-500',
      };
    }
    switch (companionState) {
      case 'SPEAKING':
        return {
          label: 'TARS SPEAKING',
          colorClass: 'bg-purple-950/80 border-purple-500/60 text-purple-200 shadow-[0_0_12px_rgba(168,85,247,0.3)]',
          dotClass: 'bg-purple-400 animate-pulse',
        };
      case 'THINKING':
        return {
          label: 'THINKING...',
          colorClass: 'bg-cyan-950/80 border-cyan-500/60 text-cyan-200 shadow-[0_0_12px_rgba(6,182,212,0.3)]',
          dotClass: 'bg-cyan-400 animate-ping',
        };
      case 'WAKE':
        return {
          label: 'WAKE DETECTED',
          colorClass: 'bg-amber-950/80 border-amber-500/60 text-amber-200 shadow-[0_0_12px_rgba(245,158,11,0.3)]',
          dotClass: 'bg-amber-400 animate-pulse',
        };
      case 'LISTENING':
        return {
          label: 'LISTENING',
          colorClass: 'bg-emerald-950/80 border-emerald-500/60 text-emerald-200 shadow-[0_0_12px_rgba(16,185,129,0.35)]',
          dotClass: 'bg-emerald-400 animate-pulse',
        };
      default:
        return {
          label: isListening ? 'LISTENING' : 'READY ("Hey TARS")',
          colorClass: isListening
            ? 'bg-emerald-950/80 border-emerald-500/60 text-emerald-200'
            : 'bg-slate-900/90 border-slate-700/80 text-slate-300',
          dotClass: isListening ? 'bg-emerald-400 animate-pulse' : 'bg-cyan-400',
        };
    }
  };

  const badge = getStatusBadge();
  const displayText = streamedText || responseSpeakerText || liveTranscript;

  return (
    <div className="w-full h-full flex flex-col bg-[#040711]/95 border border-cyan-500/20 rounded-2xl shadow-[0_20px_50px_rgba(0,0,0,0.8),0_0_30px_rgba(6,182,212,0.12)] backdrop-blur-xl p-3 select-none overflow-hidden relative font-sans text-slate-100 animate-fade-in">
      {/* Top Header Bar */}
      <div className="flex items-center justify-between gap-2 pb-2 border-b border-slate-800/80 shrink-0">
        {/* Left: TARS Title & Dynamic Status Pill */}
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1.5">
            <span className="font-mono text-xs font-black tracking-widest text-cyan-300">
              TARS
            </span>
            <span className="text-[10px] text-slate-400 font-mono">VOICE</span>
          </div>

          <div
            className={`flex items-center gap-1.5 px-2 py-0.5 rounded-full border text-[9px] font-mono font-semibold transition-all ${badge.colorClass}`}
          >
            <div className={`w-1.5 h-1.5 rounded-full ${badge.dotClass}`} />
            <span>{badge.label}</span>
          </div>
        </div>

        {/* Center: Wake Status Pill */}
        {wakeStatus && (
          <button
            type="button"
            onClick={onToggleWakeListening}
            disabled={!onToggleWakeListening}
            className={`flex items-center gap-1 px-1.5 py-0.5 rounded-full border text-[9px] font-mono transition-colors ${
              wakeStatus.isActive
                ? 'bg-emerald-950/50 border-emerald-500/40 text-emerald-300'
                : 'bg-slate-900 border-slate-700 text-slate-400 hover:border-slate-500'
            }`}
            title={wakeStatus.isActive ? 'Local Wake: Active ("Hey TARS")' : 'Wake listener muted'}
          >
            <Radio className={`w-2.5 h-2.5 ${wakeStatus.isActive ? 'text-emerald-400 animate-pulse' : 'text-slate-500'}`} />
            <span>{wakeStatus.isActive ? 'WAKE ON' : 'WAKE OFF'}</span>
          </button>
        )}

        {/* Right: Window Controls */}
        <div className="flex items-center gap-1 text-slate-400">
          {onExpandToHUD && (
            <button
              onClick={onExpandToHUD}
              className="p-1 hover:text-cyan-300 hover:bg-slate-800/60 rounded transition-colors"
              title="Expand to Full HUD"
            >
              <Minimize2 className="w-3.5 h-3.5" />
            </button>
          )}
          {onExpandToWorkstation && (
            <button
              onClick={onExpandToWorkstation}
              className="p-1 hover:text-cyan-300 hover:bg-slate-800/60 rounded transition-colors"
              title="Expand to Full Dashboard"
            >
              <Maximize2 className="w-3.5 h-3.5" />
            </button>
          )}
          {onDismiss && (
            <button
              onClick={onDismiss}
              className="p-1 hover:text-rose-400 hover:bg-rose-950/40 rounded transition-colors"
              title="Dismiss / Hide (Esc)"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          )}
        </div>
      </div>

      {/* Active Application Context Subtitle */}
      {activeContextTitle && (
        <div className="mt-1 flex items-center justify-between text-[9px] font-mono text-slate-400 shrink-0 truncate">
          <span className="truncate">Context: {activeContextTitle}</span>
          <span className="text-cyan-400/70 shrink-0 ml-1">Native Lock</span>
        </div>
      )}

      {/* Waveform Stage (Reacts in real-time to mic & speaker) */}
      <div className="my-2 h-14 w-full bg-[#02050b]/80 border border-slate-800/60 rounded-xl flex items-center justify-center relative overflow-hidden shrink-0">
        <canvas
          ref={canvasRef}
          width={380}
          height={56}
          className="w-full h-full block"
        />

        {/* In-waveform micro status cue */}
        <div className="absolute right-2 bottom-1 text-[8px] font-mono text-slate-400/80 pointer-events-none">
          {companionState === 'SPEAKING'
            ? 'TTS ACTIVE'
            : isListening
            ? 'LISTENING PCM'
            : 'VAD READY'}
        </div>
      </div>

      {/* Dynamic Content Display (Transcript / Streamed Response / Error) */}
      <div className="flex-1 min-h-0 bg-[#060b14]/70 border border-slate-800/60 rounded-xl p-2.5 overflow-y-auto custom-scrollbar flex flex-col justify-start">
        {errorMessage ? (
          <div className="flex items-start gap-2 text-rose-300 text-xs font-mono">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5 text-rose-400" />
            <div className="flex-1">
              <div className="font-bold">Voice Pipeline Error</div>
              <div className="text-[11px] text-rose-300/80 mt-0.5">{errorMessage}</div>
            </div>
          </div>
        ) : displayText ? (
          <div className="text-xs font-mono leading-relaxed text-slate-200">
            {liveTranscript && isListening && (
              <div className="text-[10px] text-emerald-400 font-semibold mb-1 flex items-center gap-1">
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
                YOU SAID:
              </div>
            )}
            {streamedText && companionState === 'THINKING' && (
              <div className="text-[10px] text-cyan-400 font-semibold mb-1 flex items-center gap-1">
                <Sparkles className="w-3 h-3 text-cyan-400 animate-pulse" />
                TARS STREAMING:
              </div>
            )}
            {companionState === 'SPEAKING' && (
              <div className="text-[10px] text-purple-400 font-semibold mb-1 flex items-center gap-1">
                <Volume2 className="w-3 h-3 text-purple-400 animate-pulse" />
                TARS:
              </div>
            )}
            <div className="whitespace-pre-wrap text-slate-100 text-[11px]">
              {displayText}
              {(companionState === 'THINKING' || isListening) && (
                <span className="inline-block w-1.5 h-3 bg-cyan-400 animate-pulse ml-0.5 align-middle" />
              )}
            </div>
          </div>
        ) : (
          <div className="h-full flex flex-col items-center justify-center text-center text-slate-400 font-mono text-[11px] py-2">
            <p className="text-slate-300 font-semibold">Say &quot;Hey TARS&quot; or speak your command</p>
            <p className="text-[9px] text-slate-500 mt-1">
              e.g. &quot;Analyze this chart&quot; · &quot;What are active setups?&quot;
            </p>
          </div>
        )}
      </div>

      {/* Bottom Action Strip: Push-to-Talk, Barge-in Interrupt, Fast Action */}
      <div className="mt-2 pt-2 border-t border-slate-800/80 flex items-center justify-between gap-2 shrink-0">
        {/* Left: Quick Push-to-talk toggle */}
        <button
          type="button"
          onClick={onTogglePushToTalk}
          className={`flex items-center gap-1.5 px-3 py-1.5 rounded-xl border text-xs font-mono font-semibold transition-all ${
            isListening
              ? 'bg-rose-950/80 border-rose-500/60 text-rose-300 hover:bg-rose-900/80'
              : 'bg-cyan-950/60 border-cyan-500/40 text-cyan-200 hover:bg-cyan-900/60 hover:border-cyan-400 shadow-[0_0_12px_rgba(6,182,212,0.15)]'
          }`}
        >
          {isListening ? (
            <>
              <MicOff className="w-3.5 h-3.5 text-rose-400" />
              <span>STOP (PTT)</span>
            </>
          ) : (
            <>
              <Mic className="w-3.5 h-3.5 text-cyan-400" />
              <span>TALK (Ctrl+Shift+V)</span>
            </>
          )}
        </button>

        {/* Center / Right: Barge-in Interrupt button when speaking */}
        {companionState === 'SPEAKING' && onInterruptSpeech ? (
          <button
            type="button"
            onClick={onInterruptSpeech}
            className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-xl bg-purple-950/80 border border-purple-500/60 text-purple-200 hover:bg-purple-900/80 text-xs font-mono font-bold animate-pulse"
            title="Click or speak to interrupt TTS"
          >
            <Square className="w-3 h-3 fill-current text-purple-300" />
            <span>INTERRUPT</span>
          </button>
        ) : (
          <div className="text-[9px] font-mono text-slate-500">
            Esc to hide · Auto-dismiss on done
          </div>
        )}
      </div>
    </div>
  );
};
