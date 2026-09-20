import React from 'react';
import { CompanionVisualState } from '../../types/companion';

interface VoiceStatusPillProps {
  state: CompanionVisualState;
}

interface PillVisual {
  label: string;
  dotColor: string;
  textColor: string;
  animated: boolean;
}

// Real-state visuals only -- `state` is driven end-to-end from the
// backend's actual Gemini Live status (connected, mic_streaming, real VAD
// activity, manual_listening_enabled) via App.tsx's polling effect, never
// guessed here. VOICE_OFF is distinct from the generic "Offline" fallback:
// Offline means Gemini Live isn't connected at all; Voice Off means it's
// connected but manual listening is off -- the mic exists but nothing
// reaches it, which the button right below this pill controls.
const VISUALS: Partial<Record<CompanionVisualState, PillVisual>> = {
  VOICE_OFF: { label: 'Voice Off', dotColor: '#9ca3af', textColor: 'text-[#6b7280]', animated: false },
  LISTENING: { label: 'Listening', dotColor: '#10b981', textColor: 'text-[#374151]', animated: true },
  HEARING: { label: 'Hearing you', dotColor: '#0d9488', textColor: 'text-[#374151]', animated: true },
  THINKING: { label: 'Thinking', dotColor: '#8b5cf6', textColor: 'text-[#374151]', animated: true },
  SPEAKING: { label: 'Speaking', dotColor: '#0ea5e9', textColor: 'text-[#374151]', animated: true },
};

const OFFLINE: PillVisual = { label: 'Offline', dotColor: '#9ca3af', textColor: 'text-[#6b7280]', animated: false };

/**
 * ONE small, minimal status pill for the main Chat screen -- a dot and a
 * word, nothing more. No orb, no glow, no floating shape; matches the
 * existing off-white TARS UI.
 */
export const VoiceStatusPill: React.FC<VoiceStatusPillProps> = ({ state }) => {
  const visual = VISUALS[state] || OFFLINE;

  return (
    <div
      className="absolute top-3 right-4 z-30 flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-white border border-[#e5e7eb] select-none pointer-events-none"
      role="status"
      aria-live="polite"
      aria-label={`Voice status: ${visual.label}`}
    >
      <span
        className={`inline-block w-1.5 h-1.5 rounded-full ${visual.animated ? 'animate-pulse' : ''}`}
        style={{ backgroundColor: visual.dotColor }}
      />
      <span className={`text-[11px] font-normal font-sans ${visual.textColor}`}>{visual.label}</span>
    </div>
  );
};
