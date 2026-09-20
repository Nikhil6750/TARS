import React from 'react';
import { Mic, Square } from 'lucide-react';

interface ManualListenButtonProps {
  enabled: boolean;
  busy?: boolean;
  onToggle: () => void;
}

/**
 * ONE minimal microphone toggle for manual listening mode -- the button
 * itself must make it obvious whether TARS can currently hear the user.
 * Toggles GeminiLiveLoop's native mic forwarding via the backend's
 * manual_listening_enabled flag (see App.tsx's handleToggleManualListening);
 * never starts a second/browser mic stream. No orb, no large animation.
 */
export const ManualListenButton: React.FC<ManualListenButtonProps> = ({
  enabled,
  busy = false,
  onToggle,
}) => {
  return (
    <button
      type="button"
      onClick={onToggle}
      disabled={busy}
      title={enabled ? 'Stop Listening' : 'Start Listening'}
      className={`flex items-center gap-1.5 px-3 py-1 rounded-full border text-[11px] font-medium font-sans transition-colors select-none ${
        enabled
          ? 'bg-emerald-50 border-emerald-500 text-emerald-700 hover:bg-emerald-100'
          : 'bg-white border-[#e5e7eb] text-[#374151] hover:border-[#d1d5db] hover:bg-[#f9fafb]'
      } ${busy ? 'opacity-60 cursor-wait' : 'cursor-pointer'}`}
    >
      {enabled ? (
        <Square className="w-3 h-3 fill-current stroke-none" />
      ) : (
        <Mic className="w-3 h-3 stroke-[1.8]" />
      )}
      <span>{enabled ? 'Stop Listening' : 'Start Listening'}</span>
    </button>
  );
};
