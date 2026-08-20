import React, { useEffect, useRef } from 'react';
import { X } from 'lucide-react';
import { CompanionVisualState } from '../../types/companion';

export type VoicePanelStatus = 'LISTENING' | 'THINKING' | 'SPEAKING' | 'IDLE';

interface VoicePanelProps {
  status: VoicePanelStatus;
  audioVolume: number;
  transcript: string;
  streamedAnswer: string;
  onDismiss: () => void;
}

const STATUS_LABEL: Record<VoicePanelStatus, string> = {
  LISTENING: 'Listening',
  THINKING: 'Thinking',
  SPEAKING: 'Speaking',
  IDLE: 'Say "Hey TARS"',
};

/**
 * The minimal voice experience: TARS label, status indicator, clean waveform,
 * live transcript / streamed answer, and dismiss button.
 */
export const VoicePanel: React.FC<VoicePanelProps> = ({
  status,
  audioVolume,
  transcript,
  streamedAnswer,
  onDismiss,
}) => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const animFrameRef = useRef<number | null>(null);
  const smoothedVolRef = useRef(0);

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

      const targetVol = status === 'LISTENING' || status === 'SPEAKING' ? Math.max(0.08, audioVolume) : 0.04;
      smoothedVolRef.current += (targetVol - smoothedVolRef.current) * 0.25;
      const currentVol = smoothedVolRef.current;

      phase += 0.05 + currentVol * 0.15;

      const numBars = 32;
      const barSpacing = width / numBars;
      const centerY = height / 2;

      for (let i = 0; i < numBars; i++) {
        const x = i * barSpacing + barSpacing / 2;
        const normX = (i / (numBars - 1)) * 2 - 1;
        const envelope = Math.max(0, 1 - normX * normX);
        const wave1 = Math.sin(phase + i * 0.35);
        const wave2 = Math.cos(phase * 0.8 + i * 0.2);
        const combined = wave1 * 0.6 + wave2 * 0.4;
        const barHeight = Math.max(3, envelope * currentVol * (height * 0.85) * (0.5 + Math.abs(combined) * 0.5));

        const gradient = ctx.createLinearGradient(0, centerY - barHeight / 2, 0, centerY + barHeight / 2);
        if (status === 'SPEAKING') {
          gradient.addColorStop(0, 'rgba(168, 85, 247, 0.95)');
          gradient.addColorStop(1, 'rgba(59, 130, 246, 0.6)');
        } else if (status === 'THINKING') {
          gradient.addColorStop(0, 'rgba(14, 165, 233, 0.95)');
          gradient.addColorStop(1, 'rgba(2, 132, 199, 0.5)');
        } else if (status === 'LISTENING') {
          gradient.addColorStop(0, 'rgba(16, 185, 129, 0.95)');
          gradient.addColorStop(1, 'rgba(5, 150, 105, 0.5)');
        } else {
          gradient.addColorStop(0, 'rgba(56, 189, 248, 0.55)');
          gradient.addColorStop(1, 'rgba(30, 58, 138, 0.2)');
        }

        ctx.fillStyle = gradient;
        ctx.beginPath();
        const barW = Math.max(2, barSpacing * 0.55);
        ctx.roundRect(x - barW / 2, centerY - barHeight / 2, barW, barHeight, barW / 2);
        ctx.fill();
      }

      animFrameRef.current = requestAnimationFrame(render);
    };

    render();
    return () => {
      if (animFrameRef.current !== null) cancelAnimationFrame(animFrameRef.current);
    };
  }, [status, audioVolume]);

  const displayText = streamedAnswer || transcript;

  return (
    <div className="w-screen h-screen flex flex-col bg-[#0c0e14]/98 border border-slate-700/80 rounded-2xl shadow-2xl backdrop-blur-xl p-3 select-none overflow-hidden font-sans text-slate-100">
      {/* Header: title, single status line, close -- window drag handle */}
      <div data-tauri-drag-region className="flex items-center justify-between gap-2 pb-2 border-b border-slate-800/80 shrink-0 cursor-move">
        <div data-tauri-drag-region className="flex items-center gap-2 min-w-0">
          <span className="font-semibold text-xs tracking-wider text-slate-200 shrink-0">TARS</span>
          <span
            className={`text-[11px] truncate ${
              status === 'SPEAKING'
                ? 'text-purple-400'
                : status === 'THINKING'
                ? 'text-cyan-400'
                : status === 'LISTENING'
                ? 'text-emerald-400'
                : 'text-slate-500'
            }`}
          >
            {STATUS_LABEL[status]}
          </span>
        </div>
        <button
          type="button"
          onClick={onDismiss}
          className="p-1 text-slate-400 hover:text-rose-400 hover:bg-rose-950/40 rounded transition-colors shrink-0"
          title="Hide (Esc)"
        >
          <X className="w-3.5 h-3.5" />
        </button>
      </div>

      {/* Waveform */}
      <div className="mt-2 h-10 w-full bg-[#080a10] border border-slate-800/60 rounded-lg overflow-hidden shrink-0">
        <canvas ref={canvasRef} width={344} height={40} className="w-full h-full block" />
      </div>

      {/* Transcript / streamed answer */}
      <div className="mt-2 flex-1 min-h-0 overflow-y-auto custom-scrollbar text-xs leading-relaxed text-slate-200">
        {displayText ? (
          <div className="whitespace-pre-wrap">
            {displayText}
            {(status === 'THINKING' || status === 'LISTENING') && (
              <span className="inline-block w-1.5 h-3 bg-cyan-400 animate-pulse ml-0.5 align-middle" />
            )}
          </div>
        ) : (
          <div className="h-full flex items-center justify-center text-center text-slate-500 text-xs">
            Say &quot;Hey TARS&quot; to start
          </div>
        )}
      </div>
    </div>
  );
};

export function toVoicePanelStatus(state: CompanionVisualState): VoicePanelStatus {
  if (state === 'SPEAKING') return 'SPEAKING';
  if (state === 'THINKING') return 'THINKING';
  if (state === 'LISTENING' || state === 'WAKE') return 'LISTENING';
  return 'IDLE';
}
