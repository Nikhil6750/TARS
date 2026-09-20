import React, { useEffect, useState } from 'react';
import { CompanionVisualState } from '../../types/companion';

interface TARSCharacterProps {
  state: CompanionVisualState;
  audioVolume?: number; // 0.0 to 1.0 from microphone or TTS
  size?: 'compact' | 'medium' | 'hero';
  className?: string;
  onClick?: () => void;
}

export const TARSCharacter: React.FC<TARSCharacterProps> = ({
  state,
  audioVolume = 0,
  size = 'hero',
  className = '',
  onClick
}) => {
  const [pulseTick, setPulseTick] = useState(0);

  // Subtle clock tick for deterministic internal visual cadence
  useEffect(() => {
    const timer = setInterval(() => {
      setPulseTick((t) => (t + 1) % 60);
    }, 100);
    return () => clearInterval(timer);
  }, []);

  // Theme color palette based on companion state
  const stateColorMap: Record<CompanionVisualState, { primary: string; glow: string; label: string }> = {
    IDLE: { primary: '#00f0ff', glow: 'rgba(0, 240, 255, 0.35)', label: 'READY' },
    WAKE: { primary: '#38bdf8', glow: 'rgba(56, 189, 248, 0.8)', label: 'WAKING UP' },
    LISTENING: { primary: '#00ff66', glow: 'rgba(0, 255, 102, 0.45)', label: 'LISTENING' },
    THINKING: { primary: '#a855f7', glow: 'rgba(168, 85, 247, 0.45)', label: 'ANALYZING' },
    SPEAKING: { primary: '#00f0ff', glow: 'rgba(0, 240, 255, 0.5)', label: 'TRANSMITTING' },
    ALERT: { primary: '#ffb700', glow: 'rgba(255, 183, 0, 0.45)', label: 'SETUP ALERT' },
    WARNING: { primary: '#ff3366', glow: 'rgba(255, 51, 102, 0.55)', label: 'RISK DETECTED' },
  };

  const { primary, glow, label } = stateColorMap[state] || stateColorMap.IDLE;

  // Compute segment heights & translations dynamically
  const isListening = state === 'LISTENING';
  const isThinking = state === 'THINKING';
  const isSpeaking = state === 'SPEAKING';
  const isWarning = state === 'WARNING' || state === 'ALERT';

  // Volume scale
  const vol = Math.max(0.05, Math.min(1.0, audioVolume));

  // Dynamic pillar heights
  const h1 = isListening ? 80 + vol * 30 * Math.sin(pulseTick * 0.5) : isThinking ? 90 + Math.sin(pulseTick * 0.4) * 15 : 95;
  const h2 = isListening ? 85 + vol * 40 * Math.cos(pulseTick * 0.4) : isThinking ? 85 + Math.cos(pulseTick * 0.5) * 20 : 95;
  const h3 = isListening ? 85 + vol * 40 * Math.sin(pulseTick * 0.3) : isThinking ? 85 + Math.sin(pulseTick * 0.3) * 20 : 95;
  const h4 = isListening ? 80 + vol * 30 * Math.cos(pulseTick * 0.6) : isThinking ? 90 + Math.cos(pulseTick * 0.4) * 15 : 95;

  const dims = {
    compact: { w: 120, h: 52, svgW: 160, svgH: 70 },
    medium: { w: 180, h: 100, svgW: 240, svgH: 130 },
    hero: { w: 280, h: 180, svgW: 320, svgH: 200 }
  }[size];

  return (
    <div
      onClick={onClick}
      className={`relative inline-flex flex-col items-center justify-center select-none cursor-pointer transition-all duration-300 ${className}`}
      style={{ filter: `drop-shadow(0 0 16px ${glow})` }}
      role="button"
      tabIndex={0}
      aria-label={`TARS Companion Character, current state: ${state}`}
    >
      <svg
        viewBox="0 0 320 180"
        width={dims.w}
        height={dims.h}
        className="overflow-visible"
        xmlns="http://www.w3.org/2000/svg"
      >
        <defs>
          <linearGradient id={`tars-pillar-grad-${state}`} x1="0%" y1="0%" x2="0%" y2="100%">
            <stop offset="0%" stopColor="#132038" />
            <stop offset="60%" stopColor="#0c1524" />
            <stop offset="100%" stopColor="#070c14" />
          </linearGradient>
          <linearGradient id="tars-screen-grad" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" stopColor={primary} stopOpacity="0.2" />
            <stop offset="50%" stopColor={primary} stopOpacity="0.8" />
            <stop offset="100%" stopColor={primary} stopOpacity="0.2" />
          </linearGradient>
        </defs>

        {/* Outer Aura */}
        <ellipse
          cx="160"
          cy="90"
          rx={isWarning ? 140 : 120}
          ry={isWarning ? 70 : 60}
          fill={glow}
          opacity={isWarning ? 0.35 : 0.15}
          className="transition-all duration-500"
        />

        {/* 4 Articulated Monoliths */}
        {/* Monolith 1 */}
        <g transform={`translate(40, ${90 - h1 / 2})`}>
          <rect
            x="0"
            y="0"
            width="50"
            height={h1}
            rx="6"
            fill={`url(#tars-pillar-grad-${state})`}
            stroke={primary}
            strokeWidth="1.5"
            strokeOpacity="0.7"
          />
          {/* Top Optical Status Node */}
          <rect x="10" y="8" width="30" height="4" rx="2" fill={primary} fillOpacity={0.85} />
          {/* Internal Grid Lines */}
          <line x1="8" y1="24" x2="42" y2="24" stroke={primary} strokeWidth="1" strokeOpacity="0.3" />
          <line x1="8" y1="36" x2="42" y2="36" stroke={primary} strokeWidth="1" strokeOpacity="0.3" />
        </g>

        {/* Monolith 2 (Left Core Display) */}
        <g transform={`translate(98, ${90 - h2 / 2})`}>
          <rect
            x="0"
            y="0"
            width="56"
            height={h2}
            rx="6"
            fill={`url(#tars-pillar-grad-${state})`}
            stroke={primary}
            strokeWidth="2"
            strokeOpacity="0.9"
          />
          {/* Main Visualizer Matrix Display */}
          <rect x="8" y="10" width="40" height="24" rx="3" fill="#04070c" stroke={primary} strokeWidth="1" strokeOpacity="0.4" />
          {/* Audio / Pulse bars */}
          <line x1="14" y1={22 - (isSpeaking ? vol * 8 : Math.sin(pulseTick * 0.3) * 4)} x2="14" y2={22 + (isSpeaking ? vol * 8 : Math.sin(pulseTick * 0.3) * 4)} stroke={primary} strokeWidth="2.5" strokeLinecap="round" />
          <line x1="22" y1={22 - (isSpeaking ? vol * 10 : Math.cos(pulseTick * 0.4) * 6)} x2="22" y2={22 + (isSpeaking ? vol * 10 : Math.cos(pulseTick * 0.4) * 6)} stroke={primary} strokeWidth="2.5" strokeLinecap="round" />
          <line x1="30" y1={22 - (isSpeaking ? vol * 9 : Math.sin(pulseTick * 0.5) * 5)} x2="30" y2={22 + (isSpeaking ? vol * 9 : Math.sin(pulseTick * 0.5) * 5)} stroke={primary} strokeWidth="2.5" strokeLinecap="round" />
          <line x1="38" y1={22 - (isSpeaking ? vol * 7 : Math.cos(pulseTick * 0.3) * 4)} x2="38" y2={22 + (isSpeaking ? vol * 7 : Math.cos(pulseTick * 0.3) * 4)} stroke={primary} strokeWidth="2.5" strokeLinecap="round" />
          {/* Lower status notch */}
          <rect x="8" y="44" width="40" height="3" rx="1" fill={primary} fillOpacity={0.6} />
        </g>

        {/* Monolith 3 (Right Core Display) */}
        <g transform={`translate(162, ${90 - h3 / 2})`}>
          <rect
            x="0"
            y="0"
            width="56"
            height={h3}
            rx="6"
            fill={`url(#tars-pillar-grad-${state})`}
            stroke={primary}
            strokeWidth="2"
            strokeOpacity="0.9"
          />
          {/* Main Visualizer Matrix Display */}
          <rect x="8" y="10" width="40" height="24" rx="3" fill="#04070c" stroke={primary} strokeWidth="1" strokeOpacity="0.4" />
          {/* Symmetric bars */}
          <line x1="14" y1={22 - (isSpeaking ? vol * 7 : Math.cos(pulseTick * 0.3) * 4)} x2="14" y2={22 + (isSpeaking ? vol * 7 : Math.cos(pulseTick * 0.3) * 4)} stroke={primary} strokeWidth="2.5" strokeLinecap="round" />
          <line x1="22" y1={22 - (isSpeaking ? vol * 9 : Math.sin(pulseTick * 0.5) * 5)} x2="22" y2={22 + (isSpeaking ? vol * 9 : Math.sin(pulseTick * 0.5) * 5)} stroke={primary} strokeWidth="2.5" strokeLinecap="round" />
          <line x1="30" y1={22 - (isSpeaking ? vol * 10 : Math.cos(pulseTick * 0.4) * 6)} x2="30" y2={22 + (isSpeaking ? vol * 10 : Math.cos(pulseTick * 0.4) * 6)} stroke={primary} strokeWidth="2.5" strokeLinecap="round" />
          <line x1="38" y1={22 - (isSpeaking ? vol * 8 : Math.sin(pulseTick * 0.3) * 4)} x2="38" y2={22 + (isSpeaking ? vol * 8 : Math.sin(pulseTick * 0.3) * 4)} stroke={primary} strokeWidth="2.5" strokeLinecap="round" />
          {/* Lower status notch */}
          <rect x="8" y="44" width="40" height="3" rx="1" fill={primary} fillOpacity={0.6} />
        </g>

        {/* Monolith 4 */}
        <g transform={`translate(226, ${90 - h4 / 2})`}>
          <rect
            x="0"
            y="0"
            width="50"
            height={h4}
            rx="6"
            fill={`url(#tars-pillar-grad-${state})`}
            stroke={primary}
            strokeWidth="1.5"
            strokeOpacity="0.7"
          />
          {/* Top Optical Status Node */}
          <rect x="10" y="8" width="30" height="4" rx="2" fill={primary} fillOpacity={0.85} />
          {/* Internal Grid Lines */}
          <line x1="8" y1="24" x2="42" y2="24" stroke={primary} strokeWidth="1" strokeOpacity="0.3" />
          <line x1="8" y1="36" x2="42" y2="36" stroke={primary} strokeWidth="1" strokeOpacity="0.3" />
        </g>

        {/* Dynamic Center Quantum Connector Ray */}
        <line
          x1="65"
          y1="90"
          x2="255"
          y2="90"
          stroke={primary}
          strokeWidth="1"
          strokeDasharray="4, 4"
          strokeOpacity={isThinking ? 0.9 : 0.4}
        />
      </svg>

      {/* State Status Badge */}
      {size !== 'compact' && (
        <div className="mt-1 flex items-center gap-2 px-3 py-1 rounded-full bg-surface/80 border border-cyan-500/20 backdrop-blur-md">
          <span
            className="w-2 h-2 rounded-full animate-ping"
            style={{ backgroundColor: primary }}
          />
          <span
            className="text-[11px] font-display-title font-semibold tracking-wider"
            style={{ color: primary }}
          >
            {label}
          </span>
          <span className="text-[10px] text-muted font-numeric">
            TARS.v1
          </span>
        </div>
      )}
    </div>
  );
};
