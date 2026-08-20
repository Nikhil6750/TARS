import React, { useEffect } from 'react';
import { Sidebar, ChatSessionMeta } from './Sidebar';
import { AppHeader } from './AppHeader';
import { ActiveTab, CompanionVisualState, ConnectionStatus, WorkspaceSection } from '../../types/companion';

interface AppShellProps {
  activeTab: ActiveTab;
  setActiveTab: (tab: ActiveTab) => void;
  workspaceSection?: WorkspaceSection;
  onSelectWorkspaceSection?: (sec: WorkspaceSection) => void;
  sessions: ChatSessionMeta[];
  activeSessionId: string;
  onSelectSession: (id: string) => void;
  onNewChat: () => void;
  onDeleteSession?: (id: string) => void;
  onClearHistory?: () => void;
  companionState: CompanionVisualState;
  connectionStatus: ConnectionStatus;
  children: React.ReactNode;
}

export const AppShell: React.FC<AppShellProps> = ({
  activeTab,
  setActiveTab,
  workspaceSection,
  onSelectWorkspaceSection,
  sessions,
  activeSessionId,
  onSelectSession,
  onNewChat,
  onClearHistory,
  companionState,
  connectionStatus,
  children,
}) => {
  // Global hotkeys: Ctrl+N for new chat
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'n') {
        e.preventDefault();
        onNewChat();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onNewChat]);

  return (
    <div className="w-screen h-screen flex flex-col bg-white text-slate-900 overflow-hidden font-sans select-none relative">
      {/* Top Application Header Bar Matching Reference */}
      <AppHeader
        activeTab={activeTab}
        setActiveTab={setActiveTab}
        workspaceSection={workspaceSection}
        onSelectWorkspaceSection={onSelectWorkspaceSection}
        companionState={companionState}
        connectionStatus={connectionStatus}
      />

      {/* Main Content Split: Left Sidebar + Center View */}
      <div className="flex-1 min-h-0 overflow-hidden relative flex">
        {/* Left Sidebar (Only visible on Chat view or persistently available) */}
        {activeTab === 'tars' && (
          <Sidebar
            sessions={sessions}
            activeSessionId={activeSessionId}
            onSelectSession={onSelectSession}
            onNewChat={onNewChat}
            onClearHistory={onClearHistory}
            isOpen={true}
          />
        )}

        {/* View Surface (Chat / Workspace / Settings) */}
        <main className="flex-1 h-full min-w-0 overflow-hidden relative bg-white">
          {children}
        </main>
      </div>
    </div>
  );
};
