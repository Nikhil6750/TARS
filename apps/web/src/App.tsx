import React, { useState, useEffect, useRef, useCallback } from 'react';
import { TARSTradingEvent } from './types/trading-event';
import { TARSAssistantMessage } from './types/assistant-message';
import {
  ActiveTab,
  CompanionVisualState,
  ConnectionState,
  AppSettings
} from './types/companion';
import {
  loadSettings,
  saveSettings,
  loadStoredAlerts,
  saveStoredAlerts,
  loadStoredChat,
  saveStoredChat
} from './services/storage';
import { TARSWebSocketClient } from './services/websocket';
import { audioService } from './services/audio';
import { sendNotification } from './services/notifications';
import { toggleCompactWindow } from './services/tauri';
import { createMockTradingEvent, createMockAssistantReply } from './services/mock-generator';

import { DesktopHeader } from './components/navigation/DesktopHeader';
import { MobileTabBar } from './components/navigation/MobileTabBar';
import { CompactHUD } from './components/navigation/CompactHUD';
import { CompanionHero } from './components/companion/CompanionHero';
import { ActiveSetupsView } from './components/setups/ActiveSetupsView';
import { AlertHistoryView } from './components/alerts/AlertHistoryView';
import { AskTARSView } from './components/assistant/AskTARSView';
import { VoiceControlView } from './components/voice/VoiceControlView';
import { MemoryView } from './components/memory/MemoryView';
import { SystemStatusView } from './components/system/SystemStatusView';
import { SettingsView } from './components/settings/SettingsView';

export const App: React.FC = () => {
  // App Settings
  const [settings, setSettings] = useState<AppSettings>(loadSettings);

  // View & UI Navigation
  const [activeTab, setActiveTab] = useState<ActiveTab>('companion');
  const [companionState, setCompanionState] = useState<CompanionVisualState>('IDLE');

  // Real-time Data Stores
  const [activeSetups, setActiveSetups] = useState<TARSTradingEvent[]>([]);
  const [alertsHistory, setAlertsHistory] = useState<TARSTradingEvent[]>(loadStoredAlerts);
  const [selectedAlert, setSelectedAlert] = useState<TARSTradingEvent | null>(null);
  const [chatMessages, setChatMessages] = useState<TARSAssistantMessage[]>(loadStoredChat);
  const [criticalWarnings, setCriticalWarnings] = useState<string[]>([]);
  const [protocolErrors, setProtocolErrors] = useState<Array<{ title: string; errors: string[] }>>([]);

  // Audio / Mic State
  const [isListening, setIsListening] = useState(false);
  const [audioVolume, setAudioVolume] = useState(0);

  // WebSocket Connection State
  const [connectionState, setConnectionState] = useState<ConnectionState>({
    status: 'connecting',
    url: settings.serverEndpoint,
    reconnectAttempts: 0,
    latencyMs: 0
  });

  const wsClientRef = useRef<TARSWebSocketClient | null>(null);

  // Save settings when changed
  const updateSettings = useCallback((newPartial: Partial<AppSettings>) => {
    setSettings((prev) => {
      const updated = { ...prev, ...newPartial };
      saveSettings(updated);
      return updated;
    });
  }, []);

  // Handle Compact Window Mode for Tauri
  useEffect(() => {
    toggleCompactWindow(settings.compactMode);
  }, [settings.compactMode]);

  // Handle incoming Trading Events
  const handleIncomingTradingEvent = useCallback((event: TARSTradingEvent) => {
    // 1. Add to alerts history
    setAlertsHistory((prev) => {
      const updated = [event, ...prev.filter((a) => a.event_id !== event.event_id)];
      saveStoredAlerts(updated);
      return updated;
    });

    // 2. Update active setups collection
    setActiveSetups((prev) => {
      // If event is IDLE or INVALIDATED or EXPIRED, we can keep or update state
      const existingIdx = prev.findIndex((s) => s.symbol === event.symbol);
      if (existingIdx >= 0) {
        const next = [...prev];
        next[existingIdx] = event;
        return next;
      }
      return [event, ...prev];
    });

    // 3. Update critical warnings
    if (event.warnings && event.warnings.length > 0) {
      setCriticalWarnings((prev) => Array.from(new Set([...event.warnings!, ...prev])).slice(0, 5));
    }

    // 4. Update companion face state based on event
    if (event.state === 'SETUP_VALID') {
      setCompanionState('ALERT');
      if (settings.audioEnabled) {
        sendNotification({
          title: `TARS Validated Setup: ${event.symbol} (${event.direction || 'LONG'})`,
          body: `Entry: ${event.entry || '—'} | R:R ${event.risk_reward ? `${event.risk_reward}R` : '—'}`
        });
      }
    } else if (event.state === 'RISK_WARNING' || event.state === 'SYSTEM_WARNING') {
      setCompanionState('WARNING');
      sendNotification({
        title: `TARS Warning: ${event.symbol}`,
        body: event.warnings?.[0] || 'Risk threshold or data quality trigger'
      });
    }

    // Return to IDLE after a short alert period
    setTimeout(() => {
      setCompanionState((current) => (current === 'ALERT' || current === 'WARNING' ? 'IDLE' : current));
    }, 4500);
  }, [settings.audioEnabled]);

  // Handle incoming Assistant Messages
  const handleIncomingAssistantMessage = useCallback((msg: TARSAssistantMessage) => {
    setChatMessages((prev) => {
      const updated = [...prev, msg];
      saveStoredChat(updated);
      return updated;
    });

    if (msg.role === 'assistant') {
      setCompanionState('SPEAKING');
      if (settings.audioEnabled) {
        audioService.speakText(msg.content, settings.speechRate, settings.speechVolume).then(() => {
          setCompanionState('IDLE');
        });
      } else {
        setTimeout(() => setCompanionState('IDLE'), 2000);
      }
    }
  }, [settings.audioEnabled, settings.speechRate, settings.speechVolume]);

  // WebSocket Connection Management
  useEffect(() => {
    const ws = new TARSWebSocketClient(settings.serverEndpoint);
    wsClientRef.current = ws;

    const unsubEvent = ws.onTradingEvent(handleIncomingTradingEvent);
    const unsubMsg = ws.onAssistantMessage(handleIncomingAssistantMessage);
    const unsubComp = ws.onCompanionState((state) => setCompanionState(state));
    const unsubConn = ws.onConnectionChange((status, latency, err) => {
      setConnectionState((prev) => ({
        ...prev,
        status,
        latencyMs: latency ?? prev.latencyMs,
        errorMessage: err
      }));
    });
    const unsubErr = ws.onProtocolError((title, errors) => {
      setProtocolErrors((prev) => [{ title, errors }, ...prev].slice(0, 20));
    });

    ws.connect();

    return () => {
      unsubEvent();
      unsubMsg();
      unsubComp();
      unsubConn();
      unsubErr();
      ws.disconnect();
    };
  }, [settings.serverEndpoint, handleIncomingTradingEvent, handleIncomingAssistantMessage]);

  // Mock Event Generator Timer (for development fixture mode)
  useEffect(() => {
    if (!settings.mockGeneratorActive) return;

    // Seed initial mock setups if list is empty
    if (activeSetups.length === 0) {
      const initial = [
        createMockTradingEvent({ symbol: 'XAUUSD', direction: 'LONG', state: 'SETUP_VALID', validation_status: 'VALID', entry: 2684.50, stop_loss: 2676.00, take_profit: 2708.50, risk_reward: 2.82, risk_percent: 1.0 }),
        createMockTradingEvent({ symbol: 'NQ', direction: 'SHORT', state: 'SETUP_DEVELOPING', validation_status: 'PENDING', entry: 20420.25, stop_loss: 20475.00, take_profit: 20265.00, risk_reward: 2.83, risk_percent: 0.75 }),
        createMockTradingEvent({ symbol: 'ES', direction: 'LONG', state: 'RISK_WARNING', validation_status: 'VALID', entry: 5880.50, stop_loss: 5865.00, take_profit: 5925.00, risk_reward: 2.87, risk_percent: 1.5 }),
      ];
      initial.forEach((evt) => handleIncomingTradingEvent(evt));
    }

    const interval = setInterval(() => {
      const mockEvent = createMockTradingEvent();
      handleIncomingTradingEvent(mockEvent);
    }, settings.mockIntervalSeconds * 1000);

    return () => clearInterval(interval);
  }, [settings.mockGeneratorActive, settings.mockIntervalSeconds, activeSetups.length, handleIncomingTradingEvent]);

  // Push to Talk Handler
  const handleTogglePushToTalk = async () => {
    if (isListening) {
      // Stop listening
      setIsListening(false);
      audioService.stopPushToTalk();
      setCompanionState('THINKING');

      // Simulate transcription & assistant response
      setTimeout(() => {
        const userVoiceMsg: TARSAssistantMessage = {
          schema_version: '1.0.0',
          message_id: crypto.randomUUID ? crypto.randomUUID() : 'msg_' + Date.now(),
          conversation_id: 'conv_voice_session',
          timestamp: new Date().toISOString(),
          role: 'user',
          content: 'What is our current gold setup and risk exposure?',
          input_mode: 'voice',
          providers: { stt: 'faster-whisper' }
        };
        handleIncomingAssistantMessage(userVoiceMsg);

        setTimeout(() => {
          const reply = createMockAssistantReply(userVoiceMsg.content, userVoiceMsg.conversation_id, activeSetups);
          handleIncomingAssistantMessage(reply);
        }, 600);
      }, 500);
    } else {
      // Start listening
      const started = await audioService.startPushToTalk((vol) => {
        setAudioVolume(vol);
      });
      if (started) {
        setIsListening(true);
        setCompanionState('LISTENING');
      }
    }
  };

  // Send Chat Message
  const handleSendMessage = (text: string, inputMode: 'text' | 'voice' = 'text') => {
    const userMsg: TARSAssistantMessage = {
      schema_version: '1.0.0',
      message_id: crypto.randomUUID ? crypto.randomUUID() : 'msg_' + Date.now(),
      conversation_id: 'conv_main_session',
      timestamp: new Date().toISOString(),
      role: 'user',
      content: text,
      input_mode: inputMode,
    };

    handleIncomingAssistantMessage(userMsg);
    setCompanionState('THINKING');

    // Send through WebSocket if connected
    if (wsClientRef.current && wsClientRef.current.getStatus() === 'connected') {
      wsClientRef.current.send({
        type: 'assistant_message',
        payload: userMsg
      });
    } else {
      // Generate simulated mock assistant reply
      setTimeout(() => {
        const reply = createMockAssistantReply(text, userMsg.conversation_id, activeSetups);
        handleIncomingAssistantMessage(reply);
      }, 500);
    }
  };

  // Switch to Setups view and inspect
  const handleInspectSetup = (setup: TARSTradingEvent) => {
    setSelectedAlert(setup);
    setActiveTab('alerts');
  };

  // Manual Trigger Mock Event
  const handleManualTriggerMock = () => {
    const evt = createMockTradingEvent();
    handleIncomingTradingEvent(evt);
  };

  // Compact Mode HUD Layout
  if (settings.compactMode) {
    return (
      <div className="w-screen h-screen bg-[#03060a] p-1 overflow-hidden">
        <CompactHUD
          companionState={companionState}
          onExpand={() => updateSettings({ compactMode: false })}
          activeSetups={activeSetups}
          criticalWarnings={criticalWarnings}
          isListening={isListening}
          onTogglePushToTalk={handleTogglePushToTalk}
          audioVolume={audioVolume}
        />
      </div>
    );
  }

  // Full Workstation Layout (Desktop & Responsive Mobile PWA)
  return (
    <div className="w-screen h-screen flex flex-col bg-[#03060a] text-slate-100 overflow-hidden font-sans select-none">
      {/* Desktop Header Navigation */}
      <DesktopHeader
        activeTab={activeTab}
        setActiveTab={setActiveTab}
        connectionStatus={connectionState.status}
        latencyMs={connectionState.latencyMs || 0}
        compactMode={settings.compactMode}
        setCompactMode={(compact) => updateSettings({ compactMode: compact })}
        activeSetupsCount={activeSetups.filter((s) => s.state === 'SETUP_VALID').length}
        unreadAlertsCount={alertsHistory.length}
      />

      {/* Main Content View Container with Safe Areas */}
      <main className="flex-1 overflow-hidden relative pb-16 lg:pb-0 safe-top">
        {activeTab === 'companion' && (
          <CompanionHero
            companionState={companionState}
            connectionStatus={connectionState.status}
            latencyMs={connectionState.latencyMs || 0}
            activeSetups={activeSetups}
            criticalWarnings={criticalWarnings}
            isListening={isListening}
            onTogglePushToTalk={handleTogglePushToTalk}
            audioVolume={audioVolume}
            onSendMessage={handleSendMessage}
            onInspectSetup={handleInspectSetup}
          />
        )}

        {activeTab === 'setups' && (
          <ActiveSetupsView
            setups={activeSetups}
            onSelectSetup={handleInspectSetup}
          />
        )}

        {activeTab === 'alerts' && (
          <AlertHistoryView
            alerts={alertsHistory}
            selectedAlert={selectedAlert}
            onSelectAlert={setSelectedAlert}
            onClearHistory={() => {
              setAlertsHistory([]);
              saveStoredAlerts([]);
            }}
          />
        )}

        {activeTab === 'chat' && (
          <AskTARSView
            messages={chatMessages}
            onSendMessage={handleSendMessage}
            isListening={isListening}
            onTogglePushToTalk={handleTogglePushToTalk}
            activeSetups={activeSetups}
            onInspectSetup={handleInspectSetup}
          />
        )}

        {activeTab === 'voice' && (
          <VoiceControlView
            isListening={isListening}
            onTogglePushToTalk={handleTogglePushToTalk}
            audioVolume={audioVolume}
            onVoiceTranscribed={(text) => handleSendMessage(text, 'voice')}
          />
        )}

        {activeTab === 'memory' && <MemoryView />}

        {activeTab === 'system' && (
          <SystemStatusView
            connectionState={connectionState}
            onUpdateEndpoint={(url) => updateSettings({ serverEndpoint: url })}
            onReconnect={() => wsClientRef.current?.connect()}
            protocolErrors={protocolErrors}
            onClearErrors={() => setProtocolErrors([])}
          />
        )}

        {activeTab === 'settings' && (
          <SettingsView
            settings={settings}
            onUpdateSettings={updateSettings}
            onTriggerMockEvent={handleManualTriggerMock}
          />
        )}
      </main>

      {/* Mobile Tab Navigation Bar (iOS PWA & Small Viewports) */}
      <MobileTabBar
        activeTab={activeTab}
        setActiveTab={setActiveTab}
        activeSetupsCount={activeSetups.filter((s) => s.state === 'SETUP_VALID').length}
        unreadAlertsCount={alertsHistory.length}
      />
    </div>
  );
};
