import React from 'react';
import { BarChart3, MessageSquare, Search } from 'lucide-react';

interface EmptyStateProps {
  onSelectPrompt: (prompt: string) => void;
  onOpenWorkspace?: () => void;
  isListening?: boolean;
}

export const EmptyState: React.FC<EmptyStateProps> = ({ onSelectPrompt, isListening = false }) => {
  const promptChips = [
    {
      label: 'Analyze current chart',
      icon: BarChart3,
      action: () => onSelectPrompt('Analyze this chart'),
    },
    {
      label: 'Ask TARS',
      icon: MessageSquare,
      action: () => onSelectPrompt('What is the current market sentiment?'),
    },
    {
      label: 'Research market',
      icon: Search,
      action: () => onSelectPrompt('Research the current market structure and key macro drivers'),
    },
  ];

  return (
    <div className="h-full flex flex-col items-center justify-center text-center px-4 py-8 max-w-xl mx-auto select-none">
      {/* ChatGPT-Voice Style Metallic Pearl Sphere with Concentric Ripples */}
      <div className="orb-container mb-5">
        <div className="orb-ripple-outer" />
        <div className="orb-ripple-mid" />
        <div className={`orb-sphere ${isListening ? 'orb-sphere-listening' : ''}`} />
      </div>

      {/* TARS Brand Title */}
      <h1 className="text-3xl font-semibold tracking-tight text-[#1f2937] mb-1.5 font-sans">
        TARS
      </h1>
      <p className="text-[15px] text-[#6b7280] mb-8 font-sans font-normal">
        How can I help?
      </p>

      {/* Minimal Prompt Chips */}
      <div className="flex flex-col sm:flex-row flex-wrap items-center justify-center gap-3 w-full">
        {promptChips.map((chip, idx) => {
          const Icon = chip.icon;
          return (
            <button
              key={idx}
              type="button"
              onClick={chip.action}
              className="flex items-center gap-2.5 px-4 py-2.5 rounded-xl bg-white hover:bg-[#f9fafb] border border-[#e5e7eb] text-[#374151] hover:text-[#111827] text-[13px] font-normal transition-all cursor-pointer shadow-[0_1px_3px_rgba(0,0,0,0.04)] hover:shadow-sm"
            >
              <Icon className="w-4 h-4 text-[#6b7280] shrink-0 stroke-[1.8]" />
              <span>{chip.label}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
};
