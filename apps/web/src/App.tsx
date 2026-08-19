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
import { toggleCompactWindow, registerGlobalShortcut, unregisterGlobalShortcut } from './services/tauri';
import { createMockTradingEvent, createMockAssistantReply } from './services/mock-generator';
import { actionRuntimeClient } from './services/actions';

import { DesktopHeader } from './components/navigation/DesktopHeader';
import { MobileTabBar } from './components/navigation/MobileTabBar';
import { CompanionHero } from './components/companion/CompanionHero';
import { ActiveSetupsView } from './components/setups/ActiveSetupsView';
import { AlertHistoryView } from './components/alerts/AlertHistoryView';
import { AskTARSView } from './components/assistant/AskTARSView';
import { VoiceControlView } from './components/voice/VoiceControlView';
import { MemoryView } from './components/memory/MemoryView';
import { SystemStatusView } from './components/system/SystemStatusView';
import { SettingsView } from './components/settings/SettingsView';
import { nativeBridge } from './services/native-bridge';
import { ChartAnalysisData } from './components/hud/ChartAnalysisCard';
import { VoiceAssistantRuntime } from './runtime/VoiceAssistantRuntime';
import { assistantClient } from './runtime/AssistantClient';

const ANALYZE_CHART_PATTERN = /\b(analy[sz]e)\s+(this|the|my)?\s*chart\b/i;

export const App: React.FC = () => {
  // App Settings
  const [settings, setSettings] = useState<AppSettings>(loadSettings);

  // View & UI Navigation
  const [activeTab, setActiveTab] = useState<ActiveTab>('companion');
  const [companionState, setCompanionState] = useState<CompanionVisualState>('IDLE');
  // Which surface the single native window is currently showing -- the
  // minimal voice-first panel (default) or the optional full dashboard.
  // Driven by native tars://summon-hud events (see WindowLifecycle), which
  // fire both from wake detection and from tray "Open Main Dashboard".
  const [appMode, setAppMode] = useState<'voice' | 'workstation'>('voice');

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

  // Text streaming in as the dashboard's own chat reply arrives, shown in
  // place of the generic THINKING placeholder until the full message lands
  // in chatMessages (see handleSendMessage).
  const [streamingAnswer, setStreamingAnswer] = useState('');
  // Stable per-session id for the dashboard's main chat conversation --
  // must be a real UUID: the backend does UUID(conversation_id) when saving
  // messages (see assistant/router.py), so a non-UUID string like the old
  // hardcoded 'conv_main_session' made every /assistant/query call fail.
  const mainChatConvIdRef = useRef<string>(crypto.randomUUID());

  // WebSocket Connection State
  const [connectionState, setConnectionState] = useState<ConnectionState>({
    status: 'connecting',
    url: settings.serverEndpoint,
    reconnectAttempts: 0,
    latencyMs: 0
  });

  const wsClientRef = useRef<TARSWebSocketClient | null>(null);
  const companionStateRef = useRef<CompanionVisualState>('IDLE');
  companionStateRef.current = companionState;

  const autoHideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const cancelAutoHide = useCallback(() => {
    if (autoHideTimerRef.current !== null) {
      clearTimeout(autoHideTimerRef.current);
      autoHideTimerRef.current = null;
    }
  }, []);

  // Auto-hide only applies to the dashboard's own legacy PTT/chat surfaces
  // (CompanionHero/AskTARSView/VoiceControlView) -- the voice-first panel
  // manages its own hide timing inside VoiceAssistantRuntime.
  const scheduleAutoHide = useCallback((delayMs = 2800) => {
    cancelAutoHide();
    if (appMode === 'voice') {
      autoHideTimerRef.current = setTimeout(() => {
        nativeBridge.hideHUD();
      }, delayMs);
    }
  }, [appMode, cancelAutoHide]);

  // Save settings when changed
  const updateSettings = useCallback((newPartial: Partial<AppSettings>) => {
    setSettings((prev) => {
      const updated = { ...prev, ...newPartial };
      saveSettings(updated);
      return updated;
    });
  }, []);

  // Request microphone permission on mount so WebView2 / browser allows voice right away
  useEffect(() => {
    audioService.requestMicrophonePermission().catch(() => {});
  }, []);

  // Handle Compact Window Mode for Tauri
  useEffect(() => {
    toggleCompactWindow(settings.compactMode);
  }, [settings.compactMode]);

  // Register Global Shortcuts (Ctrl+Shift+Space / Ctrl+Shift+T summon the
  // voice panel; Ctrl+Shift+V triggers the dashboard's own legacy PTT).
  // Escape/hide and the native tars://summon-hud -> appMode wiring live in
  // VoiceAssistantRuntime/WindowLifecycle, which are always mounted, so
  // they are not duplicated here.
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === ' ' || e.code === 'Space')) {
        e.preventDefault();
        nativeBridge.summonHUD('voice');
      } else if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === 'T' || e.key === 't')) {
        e.preventDefault();
        nativeBridge.summonHUD('voice');
      }
    };
    window.addEventListener('keydown', handleKeyDown);

    registerGlobalShortcut('CommandOrControl+Shift+Space', () => nativeBridge.summonHUD('voice'));
    registerGlobalShortcut('CommandOrControl+Shift+T', () => nativeBridge.summonHUD('voice'));

    let cleanupPtt: (() => void) | undefined;
    void (async () => {
      const { listen } = await import('@tauri-apps/api/event');
      cleanupPtt = await listen('tars://ptt-toggle', () => handleTogglePushToTalk());
    })();

    return () => {
      window.removeEventListener('keydown', handleKeyDown);
      unregisterGlobalShortcut('CommandOrControl+Shift+Space');
      unregisterGlobalShortcut('CommandOrControl+Shift+T');
      if (cleanupPtt) cleanupPtt();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Handle incoming Trading Events with lifecycle state management
  const handleIncomingTradingEvent = useCallback((event: TARSTradingEvent) => {
    // 1. Add to alerts history
    setAlertsHistory((prev) => {
      const updated = [event, ...prev.filter((a) => a.event_id !== event.event_id)];
      saveStoredAlerts(updated);
      return updated;
    });

    // 2. Update active setups collection per deterministic lifecycle
    setActiveSetups((prev) => {
      const shouldClear =
        event.state === 'IDLE' ||
        event.state === 'SETUP_INVALIDATED' ||
        event.validation_status === 'INVALID' ||
        event.validation_status === 'EXPIRED';

      if (shouldClear) {
        return prev.filter((s) => s.symbol !== event.symbol);
      }

      if (event.state === 'SETUP_DEVELOPING' || event.state === 'SETUP_VALID') {
        const existingIdx = prev.findIndex((s) => s.symbol === event.symbol);
        if (existingIdx >= 0) {
          const next = [...prev];
          next[existingIdx] = event;
          return next;
        }
        return [event, ...prev];
      }

      return prev;
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
          body: `Entry: ${event.entry || '-'} | R:R ${event.risk_reward ? `${event.risk_reward}R` : '-'}`
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
    cancelAutoHide();
    setChatMessages((prev) => {
      const updated = [...prev, msg];
      saveStoredChat(updated);
      return updated;
    });

    if (msg.role === 'assistant') {
      setCompanionState('SPEAKING');

      if (settings.audioEnabled && msg.content) {
        audioService.synthesizeAndPlay(msg.content, settings.apiEndpoint, (vol) => {
          setAudioVolume(vol);
        })
          .catch((ttsErr) => {
            console.warn('[TARS TTS] Backend synthesis error, fallback to browser synthesis:', ttsErr);
            return audioService.speakText(msg.content, settings.speechRate, settings.speechVolume, (vol) => {
              setAudioVolume(vol);
            });
          })
          .finally(() => {
            setCompanionState('IDLE');
            setAudioVolume(0);
            scheduleAutoHide(3000);
          });
      } else {
        setTimeout(() => {
          setCompanionState('IDLE');
          setAudioVolume(0);
          scheduleAutoHide(3000);
        }, 2000);
      }
    }
  }, [settings.audioEnabled, settings.apiEndpoint, settings.speechRate, settings.speechVolume, cancelAutoHide, scheduleAutoHide]);

  // "Analyze this chart": captures the active window through the real,
  // backend-authorized capture flow and the active-window context
  const isAnalyzingChartRef = useRef(false);

  const handleAnalyzeChart = useCallback(async () => {
    if (isAnalyzingChartRef.current) return;
    isAnalyzingChartRef.current = true;
    cancelAutoHide();

    const convId = 'conv_main_session';
    const newMessage = (content: string, error?: string, providerName?: string): TARSAssistantMessage => ({
      schema_version: '1.0.0',
      message_id: crypto.randomUUID ? crypto.randomUUID() : 'msg_' + Date.now(),
      conversation_id: convId,
      timestamp: new Date().toISOString(),
      role: 'assistant',
      content,
      input_mode: 'text',
      error: error ?? null,
      providers: providerName ? { assistant: providerName } : undefined,
    });

    setCompanionState('THINKING');

    try {
      const [activeContext, capture] = await Promise.all([
        nativeBridge.getActiveWindowContext(),
        nativeBridge.captureChartWindow(true),
      ]);

      if (capture.is_secure_desktop) {
        handleIncomingAssistantMessage(
          newMessage(
            "I can't capture the screen right now — a secure desktop or credential prompt is active.",
            'secure_desktop_blocked'
          )
        );
        return;
      }
      if (capture.error) {
        handleIncomingAssistantMessage(
          newMessage(`I couldn't capture the screen to analyze it: ${capture.error}`, capture.error)
        );
        return;
      }

      let streamCompleted = false;
      try {
        const streamRes = await fetch(`${settings.apiEndpoint}/api/v1/assistant/analyze-chart/stream`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ conversation_id: convId, capture, active_context: activeContext }),
        });

        if (streamRes.ok && streamRes.body) {
          const reader = streamRes.body.getReader();
          const decoder = new TextDecoder('utf-8');
          let buffer = '';
          let finalData: ChartAnalysisData | null = null;

          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';

            for (const line of lines) {
              const trimmed = line.trim();
              if (trimmed.startsWith('data: ')) {
                try {
                  const event = JSON.parse(trimmed.slice(6));
                  if (event.type === 'complete' && event.result) {
                    finalData = event.result as ChartAnalysisData;
                    streamCompleted = true;
                  }
                } catch {
                  // ignore non-json SSE frames
                }
              }
            }
          }

          if (finalData) {
            const spoken: string =
              typeof finalData.speech_text === 'string' && finalData.speech_text
                ? finalData.speech_text
                : String(finalData.market_context || 'Chart analysis complete.');
            handleIncomingAssistantMessage(newMessage(spoken, undefined, finalData.provider));
            return;
          }
        }
      } catch (streamErr) {
        console.warn('[TARS Chart Stream] Stream error, falling back to standard endpoint:', streamErr);
      }

      // Fallback: standard HTTP endpoint if stream was unavailable
      if (!streamCompleted) {
        const response = await fetch(`${settings.apiEndpoint}/api/v1/assistant/analyze-chart`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ conversation_id: convId, capture, active_context: activeContext }),
        });

        if (!response.ok) {
          const errBody = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
          const detail = typeof errBody.detail === 'string' ? errBody.detail : `HTTP ${response.status}`;
          handleIncomingAssistantMessage(newMessage(`Chart analysis failed: ${detail}`, detail));
          return;
        }

        const result = await response.json();
        const spoken: string =
          typeof result.speech_text === 'string' && result.speech_text
            ? result.speech_text
            : String(result.market_context || 'No analysis available.');
        handleIncomingAssistantMessage(newMessage(spoken, undefined, result.provider));
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      handleIncomingAssistantMessage(newMessage(`Chart analysis error: ${msg}`, msg));
    } finally {
      isAnalyzingChartRef.current = false;
    }
  }, [settings.apiEndpoint, handleIncomingAssistantMessage, cancelAutoHide]);

  // Shared routing for any already-transcribed voice utterance
  const processVoiceTranscript = useCallback(async (transcript: string) => {
    cancelAutoHide();
    const convId = 'conv_voice_session';
    setCompanionState('THINKING');

    const userVoiceMsg: TARSAssistantMessage = {
      schema_version: '1.0.0',
      message_id: crypto.randomUUID ? crypto.randomUUID() : 'msg_' + Date.now(),
      conversation_id: convId,
      timestamp: new Date().toISOString(),
      role: 'user',
      content: transcript,
      input_mode: 'voice',
      providers: { stt: 'faster-whisper' }
    };
    handleIncomingAssistantMessage(userVoiceMsg);

    try {
      actionRuntimeClient.setEndpoint(settings.apiEndpoint);
      const activeContext = await nativeBridge.getActiveWindowContext();

      const deterministicReq = actionRuntimeClient.parseDeterministicCommand(
        transcript,
        activeContext,
        'voice_ptt'
      );
      if (deterministicReq) {
        await actionRuntimeClient.submitAction(deterministicReq);
        setCompanionState('IDLE');
        scheduleAutoHide(2500);
        return;
      }

      if (ANALYZE_CHART_PATTERN.test(transcript)) {
        await handleAnalyzeChart();
        return;
      }

      const response = await fetch(`${settings.apiEndpoint}/api/v1/assistant/query`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: transcript, conversation_id: convId }),
      });

      if (response.ok) {
        const assistantReply: TARSAssistantMessage = await response.json();
        handleIncomingAssistantMessage(assistantReply);
        return;
      }
    } catch (err) {
      console.warn('[TARS Voice] Voice processing error:', err);
    }

    if (settings.mockGeneratorActive) {
      setTimeout(() => {
        const reply = createMockAssistantReply('status check', convId, activeSetups);
        handleIncomingAssistantMessage(reply);
      }, 500);
    } else {
      setCompanionState('IDLE');
      scheduleAutoHide(2500);
    }
  }, [settings.apiEndpoint, settings.mockGeneratorActive, activeSetups, handleIncomingAssistantMessage, handleAnalyzeChart, cancelAutoHide, scheduleAutoHide]);

  // Fetch initial state from HTTP backend on mount
  useEffect(() => {
    async function fetchInitialBackendState() {
      try {
        const [activeRes, historyRes] = await Promise.all([
          fetch(`${settings.apiEndpoint}/api/v1/events/active`).catch(() => null),
          fetch(`${settings.apiEndpoint}/api/v1/events?limit=50`).catch(() => null),
        ]);

        if (activeRes && activeRes.ok) {
          const active = await activeRes.json();
          if (Array.isArray(active)) {
            setActiveSetups(active);
          }
        }
        if (historyRes && historyRes.ok) {
          const history = await historyRes.json();
          if (Array.isArray(history) && history.length > 0) {
            setAlertsHistory(history);
            saveStoredAlerts(history);
          }
        }
      } catch (err) {
        console.warn('[TARS Backend HTTP] Could not pre-fetch events on boot:', err);
      }
    }

    fetchInitialBackendState();
  }, [settings.apiEndpoint]);

  // WebSocket Connection Management
  useEffect(() => {
    const ws = new TARSWebSocketClient(settings.serverEndpoint);
    wsClientRef.current = ws;

    const unsubEvent = ws.onTradingEvent(handleIncomingTradingEvent);
    const unsubSnapshot = ws.onActiveSnapshot((events) => {
      setActiveSetups(events);
    });
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
      unsubSnapshot();
      unsubMsg();
      unsubComp();
      unsubConn();
      unsubErr();
      ws.disconnect();
    };
  }, [settings.serverEndpoint, handleIncomingTradingEvent, handleIncomingAssistantMessage]);

  // Mock Event Generator Timer (only when explicitly enabled in settings)
  useEffect(() => {
    if (!settings.mockGeneratorActive) return;

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

  // Certified Push to Talk Handler
  const handleTogglePushToTalk = async () => {
    cancelAutoHide();
    if (isListening) {
      setIsListening(false);
      const audioBlob = await audioService.stopPushToTalk();
      setCompanionState('THINKING');

      if (!audioBlob || audioBlob.size === 0) {
        setCompanionState('IDLE');
        scheduleAutoHide(2000);
        return;
      }

      let transcript = '';
      try {
        transcript = await audioService.transcribeAudio(audioBlob, settings.apiEndpoint);
      } catch (err) {
        console.warn('[TARS Voice PTT] Transcription error:', err);
      }
      if (!transcript || !transcript.trim()) {
        setCompanionState('IDLE');
        scheduleAutoHide(2000);
        return;
      }
      await processVoiceTranscript(transcript.trim());
    } else {
      if (companionState === 'SPEAKING') {
        audioService.stopSpeaking();
      }
      const started = await audioService.startPushToTalk((vol) => {
        setAudioVolume(vol);
      });
      if (started) {
        setIsListening(true);
        setCompanionState('LISTENING');
      }
    }
  };

  // Send Chat Message via real backend endpoint. Streams the reply
  // (assistant/query/stream) instead of blocking on the whole response, and
  // uses a real UUID conversation id -- the backend does
  // UUID(conversation_id) when saving messages (see assistant/router.py),
  // so the old hardcoded 'conv_main_session' string made every call fail
  // with a 500 instantly, which then silently fell through to a WebSocket
  // send the backend never actually handles (app/routers/ws.py only
  // understands {"type": "ping"}) -- nothing was ever going to reply, so
  // the UI just sat on THINKING forever.
  const handleSendMessage = async (text: string, inputMode: 'text' | 'voice' = 'text') => {
    cancelAutoHide();
    const convId = mainChatConvIdRef.current;
    const userMsg: TARSAssistantMessage = {
      schema_version: '1.0.0',
      message_id: crypto.randomUUID ? crypto.randomUUID() : 'msg_' + Date.now(),
      conversation_id: convId,
      timestamp: new Date().toISOString(),
      role: 'user',
      content: text,
      input_mode: inputMode,
    };

    console.info('[CHAT] submitted');
    handleIncomingAssistantMessage(userMsg);

    if (ANALYZE_CHART_PATTERN.test(text)) {
      console.info('[CHAT] route selected: chart analysis');
      await handleAnalyzeChart();
      return;
    }

    console.info('[CHAT] route selected: assistant query stream');
    setCompanionState('THINKING');
    setStreamingAnswer('');

    let gotFirstDelta = false;
    console.info('[CHAT] endpoint called: /api/v1/assistant/query/stream');
    await assistantClient.streamQuery(text, convId, settings.apiEndpoint, {
      onDelta: (chunk) => {
        if (!gotFirstDelta) {
          gotFirstDelta = true;
          console.info('[CHAT] first delta');
        }
        setStreamingAnswer((prev) => prev + chunk);
      },
      onComplete: (message) => {
        console.info('[CHAT] complete');
        setStreamingAnswer('');
        if (message) {
          handleIncomingAssistantMessage(message as unknown as TARSAssistantMessage);
        } else {
          setCompanionState('IDLE');
          scheduleAutoHide(2500);
        }
        console.info('[CHAT] render complete');
      },
      onError: (detail) => {
        console.warn('[TARS Chat API] streaming query error:', detail);
        setStreamingAnswer('');
        if (settings.mockGeneratorActive) {
          setTimeout(() => {
            const reply = createMockAssistantReply(text, convId, activeSetups);
            handleIncomingAssistantMessage(reply);
          }, 500);
        } else {
          setCompanionState('IDLE');
          scheduleAutoHide(2500);
        }
      },
    });
  };

  // Switch to Setups view and inspect
  const handleInspectSetup = (setup: TARSTradingEvent) => {
    setSelectedAlert(setup);
    setActiveTab('alerts');
  };

  // Manual Trigger Mock Event (dev only)
  const handleManualTriggerMock = () => {
    const evt = createMockTradingEvent();
    handleIncomingTradingEvent(evt);
  };

  // The voice-first panel is always mounted so wake detection keeps
  // working no matter which surface is on screen; it only renders its UI
  // when appMode === 'voice'. The full dashboard is the secondary, optional
  // screen -- reached via tray "Open Main Dashboard" -- and is only
  // mounted while appMode === 'workstation'.
  return (
    <>
      <VoiceAssistantRuntime
        apiEndpoint={settings.apiEndpoint}
        visible={appMode === 'voice'}
        onModeChange={setAppMode}
      />

      {appMode === 'workstation' && (
        <div className="w-screen h-screen flex flex-col bg-[#03060a] text-slate-100 overflow-hidden font-sans select-none">
          {/* Desktop Header Navigation */}
          <DesktopHeader
            activeTab={activeTab}
            setActiveTab={setActiveTab}
            connectionStatus={connectionState.status}
            latencyMs={connectionState.latencyMs || 0}
            compactMode={false}
            setCompactMode={(compact) => {
              if (compact) nativeBridge.summonHUD('voice');
            }}
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
                streamingAnswer={streamingAnswer}
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
              />
            )}

            {activeTab === 'chat' && (
              <AskTARSView
                messages={chatMessages}
                activeSetups={activeSetups}
                onSendMessage={handleSendMessage}
                isListening={isListening}
                onTogglePushToTalk={handleTogglePushToTalk}
                onInspectSetup={handleInspectSetup}
                apiEndpoint={settings.apiEndpoint}
              />
            )}

            {activeTab === 'voice' && (
              <VoiceControlView
                isListening={isListening}
                onTogglePushToTalk={handleTogglePushToTalk}
                audioVolume={audioVolume}
                onVoiceTranscribed={(text) => handleSendMessage(text, 'voice')}
                apiEndpoint={settings.apiEndpoint}
              />
            )}

            {activeTab === 'memory' && (
              <MemoryView
                apiEndpoint={settings.apiEndpoint}
                mockModeActive={settings.mockGeneratorActive}
              />
            )}

            {activeTab === 'system' && (
              <SystemStatusView
                connectionState={connectionState}
                onUpdateEndpoint={(url) => updateSettings({ serverEndpoint: url })}
                onReconnect={() => {
                  if (wsClientRef.current) {
                    wsClientRef.current.disconnect();
                    wsClientRef.current.connect();
                  }
                }}
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

          {/* Mobile Tab Bar Navigation */}
          <MobileTabBar
            activeTab={activeTab}
            setActiveTab={setActiveTab}
            activeSetupsCount={activeSetups.filter((s) => s.state === 'SETUP_VALID').length}
            unreadAlertsCount={alertsHistory.length}
          />
        </div>
      )}
    </>
  );
};
