import React from 'react';
import { AppWindow, RefreshCw } from 'lucide-react';
import { ActiveWindowContext } from '../../types/actions';

interface ActiveContextBarProps {
  activeContext: ActiveWindowContext | null;
  onRefresh?: () => void;
  isLoading?: boolean;
}

export const ActiveContextBar: React.FC<ActiveContextBarProps> = ({
  activeContext,
  onRefresh,
  isLoading = false,
}) => {
  return (
    <div className="flex items-center justify-between px-3 py-1.5 bg-[#09111e]/90 border border-cyan-500/20 rounded-lg text-xs font-mono text-slate-300">
      <div className="flex items-center gap-2 min-w-0 flex-1">
        <AppWindow className="w-3.5 h-3.5 text-cyan-400 shrink-0" />
        <div className="flex items-center gap-1.5 min-w-0 truncate">
          <span className="text-slate-500 shrink-0">ACTIVE:</span>
          {activeContext ? (
            <>
              <span className="font-semibold text-cyan-300 shrink-0 px-1 py-0.2 bg-cyan-950/60 rounded border border-cyan-500/30 text-[11px]">
                {activeContext.executable}
              </span>
              <span className="text-slate-300 truncate" title={activeContext.window_title}>
                {activeContext.window_title || '(Untitled)'}
              </span>
            </>
          ) : (
            <span className="text-slate-500 italic">No foreground window captured</span>
          )}
        </div>
      </div>

      {onRefresh && (
        <button
          onClick={onRefresh}
          disabled={isLoading}
          className="ml-2 p-1 text-slate-500 hover:text-cyan-400 transition-colors disabled:opacity-50"
          title="Refresh Active Window Snapshot"
        >
          <RefreshCw className={`w-3 h-3 ${isLoading ? 'animate-spin text-cyan-400' : ''}`} />
        </button>
      )}
    </div>
  );
};
