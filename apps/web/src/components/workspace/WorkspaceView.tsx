import React from 'react';
import { Activity, Layers, Bell, Mic, Database, Cpu } from 'lucide-react';
import { WorkspaceSection, CompanionVisualState, ConnectionState } from '../../types/companion';
import { TARSTradingEvent } from '../../types/trading-event';
import { CompanionHero } from '../companion/CompanionHero';
import { ActiveSetupsView } from '../setups/ActiveSetupsView';
import { AlertHistoryView } from '../alerts/AlertHistoryView';
import { VoiceControlView } from '../voice/VoiceControlView';
import { MemoryView } from '../memory/MemoryView';
import { SystemStatusView } from '../system/SystemStatusView';

interface WorkspaceViewProps {
  section: WorkspaceSection;
  setSection: (section: WorkspaceSection) => void;
  companionState: CompanionVisualState;
  connectionState: ConnectionState;
  activeSetups: TARSTradingEvent[];
  alertsHistory: TARSTradingEvent[];
  selectedAlert: TARSTradingEvent | null;
  setSelectedAlert: (alert: TARSTradingEvent | null) => void;
  criticalWarnings: string[];
  isListening: boolean;
  onTogglePushToTalk: () => void;
  audioVolume: number;
  onSendMessage: (text: string, inputMode?: 'text' | 'voice') => void;
  onInspectSetup: (setup: TARSTradingEvent) => void;
  streamingAnswer: string;
  apiEndpoint: string;
  mockModeActive: boolean;
  onUpdateEndpoint: (url: string) => void;
  onReconnect: () => void;
  protocolErrors: Array<{ title: string; errors: string[] }>;
  onClearErrors: () => void;
}

/**
 * The quant workspace, demoted from the default screen to a secondary one
 * (see TARS MASTER MILESTONE Phase 7) -- everything that used to be top-level
 * navigation lives here now behind its own compact sub-nav, unchanged and
 * fully functional, just no longer the first thing you see.
 */
export const WorkspaceView: React.FC<WorkspaceViewProps> = ({
  section,
  setSection,
  companionState,
  connectionState,
  activeSetups,
  alertsHistory,
  selectedAlert,
  setSelectedAlert,
  criticalWarnings,
  isListening,
  onTogglePushToTalk,
  audioVolume,
  onSendMessage,
  onInspectSetup,
  streamingAnswer,
  apiEndpoint,
  mockModeActive,
  onUpdateEndpoint,
  onReconnect,
  protocolErrors,
  onClearErrors,
}) => {
  const sections: Array<{ key: WorkspaceSection; label: string; icon: React.ComponentType<{ className?: string }>; badge?: number }> = [
    { key: 'companion', label: 'Companion', icon: Activity },
    { key: 'setups', label: 'Setups', icon: Layers, badge: activeSetups.length },
    { key: 'alerts', label: 'Alerts', icon: Bell, badge: alertsHistory.length },
    { key: 'voice', label: 'Voice', icon: Mic },
    { key: 'memory', label: 'Memory', icon: Database },
    { key: 'system', label: 'System', icon: Cpu },
  ];

  return (
    <div className="w-full h-full flex flex-col overflow-hidden">
      <nav className="flex items-center gap-1 px-3 py-2 border-b border-cyan-500/10 overflow-x-auto shrink-0">
        {sections.map(({ key, label, icon: Icon, badge }) => {
          const isActive = section === key;
          return (
            <button
              key={key}
              onClick={() => setSection(key)}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium whitespace-nowrap transition-all cursor-pointer ${
                isActive
                  ? 'bg-cyan-500/20 text-cyan-300 border border-cyan-500/30'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/40 border border-transparent'
              }`}
            >
              <Icon className="w-3.5 h-3.5" />
              <span>{label}</span>
              {badge !== undefined && badge > 0 && (
                <span className="ml-0.5 px-1.5 py-0.2 rounded-full text-[10px] font-mono bg-cyan-500/30 text-cyan-200">
                  {badge}
                </span>
              )}
            </button>
          );
        })}
      </nav>

      <div className="flex-1 overflow-hidden">
        {section === 'companion' && (
          <CompanionHero
            companionState={companionState}
            connectionStatus={connectionState.status}
            latencyMs={connectionState.latencyMs || 0}
            activeSetups={activeSetups}
            criticalWarnings={criticalWarnings}
            isListening={isListening}
            onTogglePushToTalk={onTogglePushToTalk}
            audioVolume={audioVolume}
            onSendMessage={onSendMessage}
            onInspectSetup={onInspectSetup}
            streamingAnswer={streamingAnswer}
          />
        )}
        {section === 'setups' && <ActiveSetupsView setups={activeSetups} onSelectSetup={onInspectSetup} />}
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
