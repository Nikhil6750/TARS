import React from 'react';
import {
  Minimize2,
  Maximize2,
  X,
} from 'lucide-react';
import { ActiveTab, CompanionVisualState, ConnectionStatus, WorkspaceSection } from '../../types/companion';
import { isTauri, minimizeWindow, toggleMaximizeWindow, closeWindow } from '../../services/tauri';

interface AppHeaderProps {
  activeTab: ActiveTab;
  setActiveTab: (tab: ActiveTab) => void;
  workspaceSection?: WorkspaceSection;
  onSelectWorkspaceSection?: (sec: WorkspaceSection) => void;
  companionState: CompanionVisualState;
  connectionStatus: ConnectionStatus;
}

const STATE_LABEL: Partial<Record<CompanionVisualState, { text: string; dotColor: string }>> = {
  IDLE: { text: 'Ready', dotColor: 'bg-emerald-500' },
  LISTENING: { text: 'Listening...', dotColor: 'bg-emerald-500 animate-pulse' },
  THINKING: { text: 'Thinking...', dotColor: 'bg-slate-700 animate-pulse' },
  SPEAKING: { text: 'Speaking...', dotColor: 'bg-slate-800' },
  WAKE: { text: 'Waking up...', dotColor: 'bg-emerald-500' },
  ALERT: { text: 'Alert', dotColor: 'bg-amber-500' },
  WARNING: { text: 'Warning', dotColor: 'bg-rose-500' },
};

export const AppHeader: React.FC<AppHeaderProps> = ({
  activeTab,
  setActiveTab,
  workspaceSection,
  onSelectWorkspaceSection,
  companionState,
  connectionStatus,
}) => {
  const isNative = isTauri();
  const isLive = connectionStatus === 'connected';
  const stateInfo = STATE_LABEL[companionState] || {
    text: isLive ? 'Ready' : 'Offline',
    dotColor: isLive ? 'bg-emerald-500' : 'bg-slate-400',
  };

  return (
    <header
      data-tauri-drag-region
      className="h-14 bg-white border-b border-[#e5e7eb] flex items-center justify-between px-5 select-none shrink-0 z-40 text-xs"
    >
      {/* Left: TARS Brand Logo Mark & Title */}
      <div data-tauri-drag-region className="flex items-center gap-2.5 min-w-[190px]">
        {/* Black squircle mark matching reference */}
        <div className="w-5 h-5 rounded-md bg-[#1f2937] flex items-center justify-center text-white shadow-xs">
          <div className="w-2.5 h-2.5 rounded-full border-[1.5px] border-white" />
        </div>
        <span data-tauri-drag-region className="font-semibold text-[15px] text-[#1f2937] tracking-tight font-sans">
          TARS
        </span>
      </div>

      {/* Center: Clean Navigation Tabs Matching Reference */}
      <nav className="flex items-center justify-center gap-7 h-full">
        <button
          type="button"
          onClick={() => setActiveTab('tars')}
          className={`h-full flex items-center px-1.5 text-[14px] transition-colors cursor-pointer relative font-medium ${
            activeTab === 'tars'
              ? 'text-[#1f2937] font-semibold'
              : 'text-[#6b7280] hover:text-[#1f2937]'
          }`}
        >
          <span>Chat</span>
          {activeTab === 'tars' && (
            <span className="absolute bottom-0 left-0 right-0 h-[2px] bg-[#1f2937] rounded-t-full" />
          )}
        </button>

        <button
          type="button"
          onClick={() => {
            setActiveTab('workspace');
            if (onSelectWorkspaceSection && workspaceSection === 'memory') {
              onSelectWorkspaceSection('setups');
            }
          }}
          className={`h-full flex items-center px-1.5 text-[14px] transition-colors cursor-pointer relative font-medium ${
            activeTab === 'workspace' && workspaceSection !== 'memory'
              ? 'text-[#1f2937] font-semibold'
              : 'text-[#6b7280] hover:text-[#1f2937]'
          }`}
        >
          <span>Workspace</span>
          {activeTab === 'workspace' && workspaceSection !== 'memory' && (
            <span className="absolute bottom-0 left-0 right-0 h-[2px] bg-[#1f2937] rounded-t-full" />
          )}
        </button>

        <button
          type="button"
          onClick={() => {
            setActiveTab('workspace');
            if (onSelectWorkspaceSection) onSelectWorkspaceSection('memory');
          }}
          className={`h-full flex items-center px-1.5 text-[14px] transition-colors cursor-pointer relative font-medium ${
            activeTab === 'workspace' && workspaceSection === 'memory'
              ? 'text-[#1f2937] font-semibold'
              : 'text-[#6b7280] hover:text-[#1f2937]'
          }`}
        >
          <span>Memory</span>
          {activeTab === 'workspace' && workspaceSection === 'memory' && (
            <span className="absolute bottom-0 left-0 right-0 h-[2px] bg-[#1f2937] rounded-t-full" />
          )}
        </button>

        <button
          type="button"
          onClick={() => setActiveTab('settings')}
          className={`h-full flex items-center px-1.5 text-[14px] transition-colors cursor-pointer relative font-medium ${
            activeTab === 'settings'
              ? 'text-[#1f2937] font-semibold'
              : 'text-[#6b7280] hover:text-[#1f2937]'
          }`}
        >
          <span>Settings</span>
          {activeTab === 'settings' && (
            <span className="absolute bottom-0 left-0 right-0 h-[2px] bg-[#1f2937] rounded-t-full" />
          )}
        </button>
      </nav>

      {/* Right: Status Indicator + Native Window Controls */}
      <div className="flex items-center justify-end gap-3.5 min-w-[190px]">
        {/* Status Dot */}
        <div className="flex items-center gap-1.5 text-xs text-[#6b7280] font-normal">
          <span className={`w-1.5 h-1.5 rounded-full ${stateInfo.dotColor}`} />
          <span>{stateInfo.text}</span>
        </div>

        {/* Window controls for native desktop app */}
        {isNative && (
          <div className="flex items-center ml-2 pl-2.5 border-l border-[#e5e7eb] gap-1 text-[#9ca3af]">
            <button
              type="button"
              onClick={minimizeWindow}
              className="p-1.5 rounded-md hover:bg-[#f3f4f6] hover:text-[#1f2937] transition-colors cursor-pointer"
              title="Minimize"
            >
              <Minimize2 className="w-3.5 h-3.5 stroke-[1.6]" />
            </button>
            <button
              type="button"
              onClick={toggleMaximizeWindow}
              className="p-1.5 rounded-md hover:bg-[#f3f4f6] hover:text-[#1f2937] transition-colors cursor-pointer"
              title="Maximize"
            >
              <Maximize2 className="w-3.5 h-3.5 stroke-[1.6]" />
            </button>
            <button
              type="button"
              onClick={closeWindow}
              className="p-1.5 rounded-md hover:bg-rose-50 hover:text-rose-600 transition-colors cursor-pointer"
              title="Close"
            >
              <X className="w-3.5 h-3.5 stroke-[1.6]" />
            </button>
          </div>
        )}
      </div>
    </header>
  );
};
