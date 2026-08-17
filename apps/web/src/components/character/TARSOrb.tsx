import React, { useEffect, useRef, useState } from 'react';
import { CompanionVisualState } from '../../types/companion';

interface TARSOrbProps {
  state: CompanionVisualState;
  audioVolume?: number; // 0.0 to 1.0 (from real mic or TTS analyser)
  size?: 'compact' | 'hud' | 'medium' | 'hero';
  className?: string;
  onClick?: () => void;
  interactive?: boolean;
}

export const TARSOrb: React.FC<TARSOrbProps> = ({
  state,
  audioVolume = 0,
  size = 'hud',
  className = '',
  onClick,
  interactive = true,
}) => {
  // Smoothly interpolated volume for 60fps jitter-free reactivity
  const [smoothVolume, setSmoothVolume] = useState(0);
  const targetVolumeRef = useRef(audioVolume);
  const currentVolumeRef = useRef(0);
  const animFrameRef = useRef<number | null>(null);

  // Keep target volume ref up to date
  useEffect(() => {
    targetVolumeRef.current = Math.max(0, Math.min(1, audioVolume));
  }, [audioVolume]);

  // Spring / lerp interpolation loop for ultra-smooth amplitude reactions
  useEffect(() => {
    let active = true;

    const interpolate = () => {
      if (!active) return;
      const target = targetVolumeRef.current;
      const current = currentVolumeRef.current;
      
      // Fast attack, smooth decay
      const factor = target > current ? 0.35 : 0.15;
      const next = current + (target - current) * factor;
      currentVolumeRef.current = next;
      setSmoothVolume(next);

      animFrameRef.current = requestAnimationFrame(interpolate);
    };

    animFrameRef.current = requestAnimationFrame(interpolate);

    return () => {
      active = false;
      if (animFrameRef.current !== null) {
        cancelAnimationFrame(animFrameRef.current);
      }
    };
  }, []);

  // Sizing definitions
  const sizeMap = {
    compact: { px: 110, ringPx: 130 },
    hud: { px: 170, ringPx: 210 },
    medium: { px: 220, ringPx: 260 },
    hero: { px: 280, ringPx: 330 },
  };

  const { px, ringPx } = sizeMap[size] || sizeMap.hud;

  // Dynamic state attributes
  const isIdle = state === 'IDLE';
  const isWake = state === 'WAKE';
  const isListening = state === 'LISTENING';
  const isThinking = state === 'THINKING';
  const isSpeaking = state === 'SPEAKING';
  const isAlert = state === 'ALERT';
  const isWarning = state === 'WARNING';

  // Compute transform scale and glow intensities
  let scale = 1.0;
  let glowOpacity = 0.25;
  let glowColor = 'rgba(0, 240, 255, 0.4)';
  let auraColor = '#00f0ff';

  if (isIdle) {
    scale = 1.0;
    glowOpacity = 0.22;
    glowColor = 'rgba(0, 240, 255, 0.3)';
    auraColor = '#00f0ff';
  } else if (isWake) {
    scale = 1.12;
    glowOpacity = 0.85;
    glowColor = 'rgba(56, 189, 248, 0.9)';
    auraColor = '#38bdf8';
  } else if (isListening) {
    scale = 1.0 + smoothVolume * 0.14;
    glowOpacity = 0.4 + smoothVolume * 0.5;
    glowColor = 'rgba(0, 255, 128, 0.6)';
    auraColor = '#00ff80';
  } else if (isThinking) {
    scale = 1.03;
    glowOpacity = 0.55;
    glowColor = 'rgba(168, 85, 247, 0.55)';
    auraColor = '#a855f7';
  } else if (isSpeaking) {
    scale = 1.0 + smoothVolume * 0.12;
    glowOpacity = 0.45 + smoothVolume * 0.45;
    glowColor = 'rgba(0, 229, 255, 0.65)';
    auraColor = '#00e5ff';
  } else if (isAlert) {
    scale = 1.05;
    glowOpacity = 0.65;
    glowColor = 'rgba(255, 183, 0, 0.6)';
    auraColor = '#ffb700';
  } else if (isWarning) {
    scale = 1.06;
    glowOpacity = 0.75;
    glowColor = 'rgba(255, 51, 102, 0.7)';
    auraColor = '#ff3366';
  }

  return (
    <div
      onClick={onClick}
      className={`relative inline-flex items-center justify-center select-none ${
        interactive ? 'cursor-pointer' : ''
      } ${className}`}
      style={{
        width: ringPx,
        height: ringPx,
      }}
      role="button"
      tabIndex={0}
      aria-label={`TARS Voice Companion Orb, state: ${state}`}
    >
      {/* 1. Deep Ambient Aura Glow */}
      <div
        className="absolute inset-0 rounded-full transition-opacity duration-300 pointer-events-none"
        style={{
          background: `radial-gradient(circle, ${glowColor} 0%, rgba(3, 6, 10, 0) 70%)`,
          opacity: glowOpacity,
          transform: `scale(${isWake ? 1.4 : isListening || isSpeaking ? 1.1 + smoothVolume * 0.2 : 1.0})`,
          transition: 'transform 0.25s ease-out, opacity 0.25s ease-out',
        }}
      />

      {/* 2. Concentric Outer SVG HUD Technical Ticks & Energy Rings */}
      <svg
        className={`absolute inset-0 w-full h-full pointer-events-none transition-transform duration-700 ${
          isThinking
            ? 'animate-[spin_8s_linear_infinite]'
            : isSpeaking
            ? 'animate-[spin_24s_linear_infinite]'
            : isListening
            ? 'animate-[spin_40s_linear_infinite]'
            : 'animate-[spin_60s_linear_infinite]'
        }`}
        viewBox="0 0 200 200"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
      >
        {/* Outer reticle circle */}
        <circle
          cx="100"
          cy="100"
          r="92"
          stroke={auraColor}
          strokeWidth="1"
          strokeDasharray="4 8"
          strokeOpacity={isIdle ? 0.35 : 0.65}
        />

        {/* 4 Cardinal Crosshair Notch Marks */}
        <line x1="100" y1="2" x2="100" y2="10" stroke={auraColor} strokeWidth="1.5" strokeOpacity="0.8" />
        <line x1="100" y1="190" x2="100" y2="198" stroke={auraColor} strokeWidth="1.5" strokeOpacity="0.8" />
        <line x1="2" y1="100" x2="10" y2="100" stroke={auraColor} strokeWidth="1.5" strokeOpacity="0.8" />
        <line x1="190" y1="100" x2="198" y2="100" stroke={auraColor} strokeWidth="1.5" strokeOpacity="0.8" />

        {/* Audio Reactivity Waveform Arc Ring (Active during Listening / Speaking) */}
        {(isListening || isSpeaking) && (
          <circle
            cx="100"
            cy="100"
            r={84 + smoothVolume * 8}
            stroke={auraColor}
            strokeWidth={1.5 + smoothVolume * 2}
            strokeDasharray={`${20 + smoothVolume * 40} ${10 + (1 - smoothVolume) * 20}`}
            strokeOpacity={0.5 + smoothVolume * 0.5}
            className="transition-all duration-75"
          />
        )}

        {/* Thinking Radar Scan Arc */}
        {isThinking && (
          <circle
            cx="100"
            cy="100"
            r="86"
            stroke="url(#thinkingScanGradient)"
            strokeWidth="2.5"
            strokeDasharray="60 140"
          />
        )}

        <defs>
          <linearGradient id="thinkingScanGradient" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stopColor="#a855f7" stopOpacity="0" />
            <stop offset="50%" stopColor="#38bdf8" stopOpacity="0.8" />
            <stop offset="100%" stopColor="#a855f7" stopOpacity="1" />
          </linearGradient>
        </defs>
      </svg>

      {/* 3. Counter-Rotating Inner Gyro Ring (Thinking / Speaking) */}
      <svg
        className={`absolute inset-0 w-full h-full pointer-events-none ${
          isThinking
            ? 'animate-[spin_12s_linear_infinite_reverse]'
            : isSpeaking
            ? 'animate-[spin_30s_linear_infinite_reverse]'
            : 'hidden'
        }`}
        viewBox="0 0 200 200"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
      >
        <circle
          cx="100"
          cy="100"
          r="78"
          stroke={auraColor}
          strokeWidth="1"
          strokeDasharray="2 12"
          strokeOpacity="0.45"
        />
      </svg>

      {/* 4. Wake Shockwave Ring Burst */}
      {isWake && (
        <div
          className="absolute rounded-full border border-sky-400 pointer-events-none animate-ping"
          style={{
            width: px * 1.1,
            height: px * 1.1,
            borderColor: auraColor,
            animationDuration: '600ms',
            animationIterationCount: '1',
          }}
        />
      )}

      {/* 5. Core TARS Orb Asset */}
      <div
        className={`relative z-10 flex items-center justify-center ${
          isIdle ? 'animate-[pulse_4s_ease-in-out_infinite]' : ''
        }`}
        style={{
          width: px,
          height: px,
          transform: `scale3d(${scale}, ${scale}, 1)`,
          transition: isWake
            ? 'transform 0.35s cubic-bezier(0.34, 1.56, 0.64, 1)'
            : 'transform 0.12s ease-out',
          willChange: 'transform',
        }}
      >
        <img
          src="/assets/tars-orb.png"
          alt="TARS Quantum Voice Core"
          className="w-full h-full object-contain filter drop-shadow-[0_0_20px_rgba(0,240,255,0.4)] pointer-events-none"
          draggable={false}
        />

        {/* Ambient Overlay Tint when in special states */}
        {isListening && (
          <div
            className="absolute inset-0 rounded-full bg-emerald-500/10 pointer-events-none mix-blend-color-dodge transition-opacity duration-150"
            style={{ opacity: 0.3 + smoothVolume * 0.7 }}
          />
        )}
        {isThinking && (
          <div className="absolute inset-0 rounded-full bg-purple-500/15 pointer-events-none mix-blend-color-dodge animate-pulse" />
        )}
        {isWarning && (
          <div className="absolute inset-0 rounded-full bg-rose-500/20 pointer-events-none mix-blend-color-dodge animate-pulse" />
        )}
        {isAlert && (
          <div className="absolute inset-0 rounded-full bg-amber-500/20 pointer-events-none mix-blend-color-dodge animate-pulse" />
        )}
      </div>

      {/* 6. Active State Indicator Pill Below Orb */}
      <div
        className="absolute -bottom-3 left-1/2 -translate-x-1/2 z-20 px-2 py-0.5 rounded-full border text-[9px] font-mono font-semibold tracking-wider uppercase backdrop-blur-md transition-all duration-200 pointer-events-none whitespace-nowrap shadow-lg"
        style={{
          borderColor: auraColor,
          backgroundColor: 'rgba(3, 6, 12, 0.88)',
          color: auraColor,
          boxShadow: `0 0 10px ${glowColor}`,
        }}
      >
        {isIdle && '● TARS IDLE'}
        {isWake && '⚡ WAKING'}
        {isListening && `🎙️ LISTENING ${smoothVolume > 0.05 ? '●' : '...'}`}
        {isThinking && '🧠 ANALYZING...'}
        {isSpeaking && '🔊 TRANSMITTING'}
        {isAlert && '⚠️ ALERT'}
        {isWarning && '🛑 RISK DETECTED'}
      </div>
    </div>
  );
};
