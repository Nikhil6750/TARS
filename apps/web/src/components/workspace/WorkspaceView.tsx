import React from 'react';
import { Layers, Bell, Mic, Database, Cpu } from 'lucide-react';
import { WorkspaceSection, ConnectionState } from '../../types/companion';
import { TARSTradingEvent } from '../../types/trading-event';
import { ActiveSetupsView } from '../setups/ActiveSetupsView';
import { AlertHistoryView } from '../alerts/AlertHistoryView';
import { VoiceControlView } from '../voice/VoiceControlView';
import { MemoryView } from '../memory/MemoryView';
import { SystemStatusView } from '../system/SystemStatusView';

interface WorkspaceViewProps {
  section: WorkspaceSection;
  setSection: (section: WorkspaceSection) => void;
  connectionState: ConnectionState;
  activeSetups: TARSTradingEvent[];
  alertsHistory: TARSTradingEvent[];
  selectedAlert: TARSTradingEvent | null;
  setSelectedAlert: (alert: TARSTradingEvent | null) => void;
  isListening: boolean;
  onTogglePushToTalk: () => void;
  audioVolume: number;
  onSendMessage: (text: string, inputMode?: 'text' | 'voice') => void;
  onInspectSetup: (setup: TARSTradingEvent) => void;
  apiEndpoint: string;
  mockModeActive: boolean;
  onUpdateEndpoint: (url: string) => void;
  onReconnect: () => void;
  protocolErrors: Array<{ title: string; errors: string[] }>;
  onClearErrors: () => void;
}

export const WorkspaceView: React.FC<WorkspaceViewProps> = ({
  section = 'setups',
  setSection,
  connectionState,
  activeSetups,
  alertsHistory,
  selectedAlert,
  setSelectedAlert,
  isListening,
  onTogglePushToTalk,
  audioVolume,
  onSendMessage,
  onInspectSetup,
  apiEndpoint,
  mockModeActive,
  onUpdateEndpoint,
  onReconnect,
  protocolErrors,
  onClearErrors,
}) => {
  const sections: Array<{
    key: WorkspaceSection;
    label: string;
    icon: React.ComponentType<{ className?: string }>;
    badge?: number;
  }> = [
    { key: 'setups', label: 'Active Setups', icon: Layers, badge: activeSetups.length },
    { key: 'alerts', label: 'Alerts', icon: Bell, badge: alertsHistory.length },
    { key: 'memory', label: 'Memory & Research', icon: Database },
    { key: 'voice', label: 'Voice Control', icon: Mic },
    { key: 'system', label: 'Diagnostics', icon: Cpu },
  ];

  return (
    <div className="w-full h-full flex flex-col overflow-hidden bg-[#0c0e14]">
      {/* Workspace Secondary Navigation Bar */}
      <nav className="flex items-center gap-2 px-4 py-2.5 bg-[#10131b] border-b border-slate-800 shrink-0 select-none">
        <span className="text-xs font-semibold text-slate-400 mr-2 font-mono uppercase tracking-wider">
          Workspace
        </span>

        {sections.map(({ key, label, icon: Icon, badge }) => {
          const isActive = section === key || (section === 'companion' && key === 'setups');
          return (
            <button
              key={key}
              type="button"
              onClick={() => setSection(key)}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium whitespace-nowrap transition-all cursor-pointer ${
                isActive
                  ? 'bg-[#1b2232] text-cyan-300 border border-cyan-500/30 shadow-sm'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/50 border border-transparent'
              }`}
            >
              <Icon className="w-3.5 h-3.5" />
              <span>{label}</span>
              {badge !== undefined && badge > 0 && (
                <span className="ml-1 px-1.5 py-0.2 rounded-full text-[10px] font-mono bg-cyan-950/80 text-cyan-300 border border-cyan-500/20">
                  {badge}
                </span>
              )}
            </button>
          );
        })}
      </nav>

      {/* Workspace Active Section Content */}
      <div className="flex-1 min-h-0 overflow-hidden">
        {(section === 'setups' || section === 'companion') && (
          <ActiveSetupsView setups={activeSetups} onSelectSetup={onInspectSetup} />
        )}
        {section === 'alerts' && (
          <AlertHistoryView alerts={alertsHistory} selectedAlert={selectedAlert} onSelectAlert={setSelectedAlert} />
        )}
        {section === 'voice' && (
          <VoiceControlView
            isListening={isListening}
            onTogglePushToTalk={onTogglePushToTalk}
            audioVolume={audioVolume}
            onVoiceTranscribed={(text) => onSendMessage(text, 'voice')}
            apiEndpoint={apiEndpoint}
          />
        )}
        {section === 'memory' && <MemoryView apiEndpoint={apiEndpoint} mockModeActive={mockModeActive} />}
        {section === 'system' && (
          <SystemStatusView
            connectionState={connectionState}
            onUpdateEndpoint={onUpdateEndpoint}
            onReconnect={onReconnect}
            protocolErrors={protocolErrors}
            onClearErrors={onClearErrors}
          />
        )}
      </div>
    </div>
  );
};
