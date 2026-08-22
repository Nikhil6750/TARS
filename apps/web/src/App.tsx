import React, { useState, useEffect, useRef, useCallback } from 'react';
import { TARSTradingEvent } from './types/trading-event';
import { TARSAssistantMessage } from './types/assistant-message';
import {
  ActiveTab,
  WorkspaceSection,
  CompanionVisualState,
  ConnectionState,
  AppSettings
} from './types/companion';
import {
  loadSettings,
  saveSettings,
  loadStoredAlerts,
  saveStoredAlerts,
  loadStoredSessions,
  saveStoredSessions,
  StoredChatSession,
} from './services/storage';
import { TARSWebSocketClient } from './services/websocket';
import { audioService } from './services/audio';
import { sendNotification } from './services/notifications';
import { toggleCompactWindow, registerGlobalShortcut, unregisterGlobalShortcut, isTauri } from './services/tauri';
import { createMockTradingEvent, createMockAssistantReply } from './services/mock-generator';
import { nativeBridge } from './services/native-bridge';
import { VoiceAssistantRuntime } from './runtime/VoiceAssistantRuntime';
import { assistantClient } from './runtime/AssistantClient';

import { AppShell } from './components/shell/AppShell';
import { ConversationView } from './components/assistant/ConversationView';
import { WorkspaceView } from './components/workspace/WorkspaceView';
import { SettingsView } from './components/settings/SettingsView';

export const App: React.FC = () => {
  // App Settings
  const [settings, setSettings] = useState<AppSettings>(loadSettings);

  // View & Navigation State
  const [activeTab, setActiveTab] = useState<ActiveTab>('tars');
  const [workspaceSection, setWorkspaceSection] = useState<WorkspaceSection>('setups');
  const [companionState, setCompanionState] = useState<CompanionVisualState>('IDLE');

  // Surface mode: 'workstation' is the primary desktop companion UI
  const [appMode, setAppMode] = useState<'voice' | 'workstation'>('workstation');

  // Multi-session chat management
  const [sessions, setSessions] = useState<StoredChatSession[]>(() => {
    const loaded = loadStoredSessions();
    if (loaded.length > 0) return loaded;
    const initialId = crypto.randomUUID();
    return [
      {
        id: initialId,
        title: 'New Conversation',
        createdAt: new Date().toISOString(),
        messages: [],
      },
    ];
  });
  const [activeSessionId, setActiveSessionId] = useState<string>(() => sessions[0]?.id || crypto.randomUUID());

  // Real-time Data Stores
  const [activeSetups, setActiveSetups] = useState<TARSTradingEvent[]>([]);
  const [alertsHistory, setAlertsHistory] = useState<TARSTradingEvent[]>(loadStoredAlerts);
  const [selectedAlert, setSelectedAlert] = useState<TARSTradingEvent | null>(null);
  const [protocolErrors, setProtocolErrors] = useState<Array<{ title: string; errors: string[] }>>([]);

  // Audio / Mic State
  const [isListening, setIsListening] = useState(false);
  const [audioVolume, setAudioVolume] = useState(0);

  // Text streaming in as assistant reply arrives
  const [streamingAnswer, setStreamingAnswer] = useState('');
  const [analysisProgress, setAnalysisProgress] = useState<string | undefined>(undefined);

  // WebSocket Connection State
  const [connectionState, setConnectionState] = useState<ConnectionState>({
    status: 'connecting',
    url: settings.serverEndpoint,
    reconnectAttempts: 0,
    latencyMs: 0,
  });

  const wsClientRef = useRef<TARSWebSocketClient | null>(null);
  const autoHideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    document.title = 'TARS Ready';
  }, []);

  const cancelAutoHide = useCallback(() => {
    if (autoHideTimerRef.current !== null) {
      clearTimeout(autoHideTimerRef.current);
      autoHideTimerRef.current = null;
    }
  }, []);

  const scheduleAutoHide = useCallback(
    (delayMs = 2800) => {
      cancelAutoHide();
      if (appMode === 'voice') {
        autoHideTimerRef.current = setTimeout(() => {
          nativeBridge.hideHUD();
        }, delayMs);
      }
    },
    [appMode, cancelAutoHide]
  );

  // Save settings when changed
  const updateSettings = useCallback((newPartial: Partial<AppSettings>) => {
    setSettings((prev) => {
      const updated = { ...prev, ...newPartial };
      saveSettings(updated);
      return updated;
    });
  }, []);

  // Request microphone permission on mount
  useEffect(() => {
    audioService.requestMicrophonePermission().catch(() => {});
  }, []);

  // Handle Compact Window Mode for Tauri
  useEffect(() => {
    toggleCompactWindow(settings.compactMode);
  }, [settings.compactMode]);

  // Active session messages helper
  const currentSession = sessions.find((s) => s.id === activeSessionId) || sessions[0];
  const chatMessages = currentSession ? currentSession.messages : [];

  // Helper to append message to active session
  const appendMessageToActiveSession = useCallback(
    (msg: TARSAssistantMessage) => {
      setSessions((prev) => {
        const idx = prev.findIndex((s) => s.id === msg.conversation_id);
        let updated: StoredChatSession[];
        if (idx >= 0) {
          const current = prev[idx];
          const newMessages = [...current.messages, msg];
          let newTitle = current.title;
          if (current.title === 'New Conversation' && msg.role === 'user') {
            newTitle = msg.content.slice(0, 28) + (msg.content.length > 28 ? '...' : '');
          }
          const updatedSession = { ...current, title: newTitle, messages: newMessages };
          updated = [...prev];
          updated[idx] = updatedSession;
        } else {
          const newSession: StoredChatSession = {
            id: msg.conversation_id,
            title: msg.role === 'user' ? msg.content.slice(0, 28) : 'Conversation',
            createdAt: new Date().toISOString(),
            messages: [msg],
          };
          updated = [newSession, ...prev];
        }
        saveStoredSessions(updated);
        return updated;
      });
    },
    []
  );

  // Start new conversation session
  const handleNewChat = useCallback(() => {
    const newId = crypto.randomUUID();
    const newSession: StoredChatSession = {
      id: newId,
      title: 'New Conversation',
      createdAt: new Date().toISOString(),
      messages: [],
    };
    setSessions((prev) => {
      const updated = [newSession, ...prev];
      saveStoredSessions(updated);
      return updated;
    });
    setActiveSessionId(newId);
    setActiveTab('tars');
    setStreamingAnswer('');
    setAnalysisProgress(undefined);
  }, []);

  // Delete a conversation session
  const handleDeleteSession = useCallback(
    (id: string) => {
      setSessions((prev) => {
        const filtered = prev.filter((s) => s.id !== id);
        const nextSessions =
          filtered.length > 0
            ? filtered
            : [
                {
                  id: crypto.randomUUID(),
                  title: 'New Conversation',
                  createdAt: new Date().toISOString(),
                  messages: [],
                },
              ];
        saveStoredSessions(nextSessions);
        if (activeSessionId === id) {
          setActiveSessionId(nextSessions[0].id);
        }
        return nextSessions;
      });
    },
    [activeSessionId]
  );

  // Clear all conversation history
  const handleClearHistory = useCallback(() => {
    const newId = crypto.randomUUID();
    const freshSession: StoredChatSession = {
      id: newId,
      title: 'New Conversation',
      createdAt: new Date().toISOString(),
      messages: [],
    };
    setSessions([freshSession]);
    saveStoredSessions([freshSession]);
    setActiveSessionId(newId);
    setActiveTab('tars');
    setStreamingAnswer('');
    setAnalysisProgress(undefined);
  }, []);

  // Handle incoming Trading Events with lifecycle state management
  const handleIncomingTradingEvent = useCallback(
    (event: TARSTradingEvent) => {
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

      // 3. Update companion face state based on event
      if (event.state === 'SETUP_VALID') {
        setCompanionState('ALERT');
        if (settings.audioEnabled) {
          sendNotification({
            title: `TARS Validated Setup: ${event.symbol} (${event.direction || 'LONG'})`,
            body: `Entry: ${event.entry || '-'} | R:R ${event.risk_reward ? `${event.risk_reward}R` : '-'}`,
          });
        }
      } else if (event.state === 'RISK_WARNING' || event.state === 'SYSTEM_WARNING') {
        setCompanionState('WARNING');
        sendNotification({
          title: `TARS Warning: ${event.symbol}`,
          body: event.warnings?.[0] || 'Risk threshold or data quality trigger',
        });
      }

      setTimeout(() => {
        setCompanionState((current) => (current === 'ALERT' || current === 'WARNING' ? 'IDLE' : current));
      }, 4500);
    },
    [settings.audioEnabled]
  );

  // Handle incoming Assistant Messages
  const handleIncomingAssistantMessage = useCallback(
    (msg: TARSAssistantMessage) => {
      cancelAutoHide();
      appendMessageToActiveSession(msg);

      if (msg.role === 'assistant') {
        setCompanionState('IDLE');
        setAudioVolume(0);
        scheduleAutoHide(3000);
      }
    },
    [
      cancelAutoHide,
      scheduleAutoHide,
      appendMessageToActiveSession,
    ]
  );

  /* Deprecated duplicate execution path (inactive): chart capture, intent
   * selection, and voice-command execution now belong to the backend
   * AssistantTurnController. Retained in this commit only as migration
   * history while workstation chart rendering remains intact.
  // "Analyze this chart": captures active window and runs analysis
  const isAnalyzingChartRef = useRef(false);

  const handleAnalyzeChart = useCallback(async (userText?: string) => {
    if (isAnalyzingChartRef.current) return;
    isAnalyzingChartRef.current = true;
    cancelAutoHide();
    // The user's full request (e.g. "...and estimate the profit if my
    // capital was 10000 rupees") must reach the backend, not just the
    // generic default goal -- otherwise TARS silently drops half of what
    // was actually asked.
    const goal = userText?.trim() || undefined;

    const convId = activeSessionId;
    const newMessage = (content: string, error?: string, providerName?: string): TARSAssistantMessage => ({
      schema_version: '1.0.0',
      message_id: crypto.randomUUID(),
      conversation_id: convId,
      timestamp: new Date().toISOString(),
      role: 'assistant',
      content,
      input_mode: 'text',
      error: error ?? null,
      providers: providerName ? { assistant: providerName } : undefined,
    });

    setCompanionState('THINKING');
    setAnalysisProgress('Looking at the chart...');

    try {
      const [activeContext, capture] = await Promise.all([
        nativeBridge.getActiveWindowContext(),
        nativeBridge.captureChartWindow(true),
      ]);

      if (capture.is_secure_desktop) {
        setAnalysisProgress(undefined);
        handleIncomingAssistantMessage(
          newMessage(
            "I can't capture the screen right now — a secure desktop or credential prompt is active.",
            'secure_desktop_blocked'
          )
        );
        return;
      }
      if (capture.error) {
        setAnalysisProgress(undefined);
        handleIncomingAssistantMessage(
          newMessage(`I couldn't capture the screen to analyze it: ${capture.error}`, capture.error)
        );
        return;
      }

      setAnalysisProgress('Reading the chart...');

      let streamCompleted = false;
      try {
        const streamRes = await fetch(`${settings.apiEndpoint}/api/v1/assistant/analyze-chart/stream`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ conversation_id: convId, capture, active_context: activeContext, goal }),
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
            setAnalysisProgress(undefined);
            // The full structured breakdown, not the short speech-only
            // summary -- the chat bubble must show the complete analysis
            // (headings/zones/levels), not a flattened one-line paragraph.
            const displayText: string =
              typeof finalData.formatted_tars_text === 'string' && finalData.formatted_tars_text
                ? finalData.formatted_tars_text
                : typeof finalData.speech_text === 'string' && finalData.speech_text
                ? finalData.speech_text
                : String(finalData.market_context || 'Chart analysis complete.');
            handleIncomingAssistantMessage(newMessage(displayText, undefined, finalData.provider));
            return;
          }
        }
      } catch (streamErr) {
        console.warn('[TARS Chart Stream] Stream error, falling back to standard endpoint:', streamErr);
      }

      // Fallback HTTP endpoint if stream was unavailable
      if (!streamCompleted) {
        const response = await fetch(`${settings.apiEndpoint}/api/v1/assistant/analyze-chart`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ conversation_id: convId, capture, active_context: activeContext, goal }),
        });

        setAnalysisProgress(undefined);

        if (!response.ok) {
          const errBody = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
          const detail = typeof errBody.detail === 'string' ? errBody.detail : `HTTP ${response.status}`;
          handleIncomingAssistantMessage(newMessage(`Chart analysis failed: ${detail}`, detail));
          return;
        }

        const result = await response.json();
        const displayText: string =
          typeof result.formatted_tars_text === 'string' && result.formatted_tars_text
            ? result.formatted_tars_text
            : typeof result.speech_text === 'string' && result.speech_text
            ? result.speech_text
            : String(result.market_context || 'No analysis available.');
        handleIncomingAssistantMessage(newMessage(displayText, undefined, result.provider));
      }
    } catch (err) {
      setAnalysisProgress(undefined);
      const msg = err instanceof Error ? err.message : String(err);
      handleIncomingAssistantMessage(newMessage(`Chart analysis error: ${msg}`, msg));
    } finally {
      setAnalysisProgress(undefined);
      isAnalyzingChartRef.current = false;
    }
  }, [activeSessionId, settings.apiEndpoint, handleIncomingAssistantMessage, cancelAutoHide]);

  // Shared routing for transcribed voice utterance
  const processVoiceTranscript = useCallback(
    async (transcript: string) => {
      cancelAutoHide();
      const convId = activeSessionId;
      setCompanionState('THINKING');

      const userVoiceMsg: TARSAssistantMessage = {
        schema_version: '1.0.0',
        message_id: crypto.randomUUID(),
        conversation_id: convId,
        timestamp: new Date().toISOString(),
        role: 'user',
        content: transcript,
        input_mode: 'voice',
        providers: { stt: 'faster-whisper' },
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
          await handleAnalyzeChart(transcript);
          return;
        }

        const result = await assistantClient.query(
          transcript,
          convId,
          settings.apiEndpoint
        );
        const assistantReply: TARSAssistantMessage = {
          ...result.message,
          content: result.display_text || result.message.content,
          display_text: result.display_text,
          speech_text: result.speech_text,
        };
        handleIncomingAssistantMessage(assistantReply);
        return;
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
    },
    [
      activeSessionId,
      settings.apiEndpoint,
      settings.mockGeneratorActive,
      activeSetups,
      handleIncomingAssistantMessage,
      handleAnalyzeChart,
      cancelAutoHide,
      scheduleAutoHide,
    ]
  );
  */

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
        errorMessage: err,
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

  // Mock Event Generator Timer (only when explicitly enabled)
  useEffect(() => {
    if (!settings.mockGeneratorActive) return;

    if (activeSetups.length === 0) {
      const initial = [
        createMockTradingEvent({
          symbol: 'XAUUSD',
          direction: 'LONG',
          state: 'SETUP_VALID',
          validation_status: 'VALID',
          entry: 2684.5,
          stop_loss: 2676.0,
          take_profit: 2708.5,
          risk_reward: 2.82,
          risk_percent: 1.0,
        }),
        createMockTradingEvent({
          symbol: 'NQ',
          direction: 'SHORT',
          state: 'SETUP_DEVELOPING',
          validation_status: 'PENDING',
          entry: 20420.25,
          stop_loss: 20475.0,
          take_profit: 20265.0,
          risk_reward: 2.83,
          risk_percent: 0.75,
        }),
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

      try {
        const response = await audioService.submitUtterance(
          audioBlob,
          settings.apiEndpoint,
          activeSessionId,
          `ptt-${activeSessionId}`
        );
        if (response.status === 'ignored') {
          setCompanionState('IDLE');
          return;
        }
        if (response.transcript) {
          handleIncomingAssistantMessage({
            schema_version: '1.0.0',
            message_id: `${response.turn_id}-user`,
            conversation_id: response.conversation_id,
            timestamp: new Date().toISOString(),
            role: 'user',
            content: response.transcript,
            input_mode: 'voice',
            providers: { stt: 'backend' },
          });
        }
        handleIncomingAssistantMessage({
          schema_version: '1.0.0',
          message_id: response.turn_id,
          conversation_id: response.conversation_id,
          timestamp: new Date().toISOString(),
          role: 'assistant',
          content: response.display_text,
          display_text: response.display_text,
          speech_text: response.speech_text,
          input_mode: 'voice',
          intent: response.intent,
          providers: { assistant: response.provider, tts: 'backend' },
        });
        if (response.audio_chunks_base64.length > 0) {
          setCompanionState('SPEAKING');
          await audioService.playBase64Chunks(response.audio_chunks_base64, setAudioVolume);
        }
        setCompanionState(response.status === 'awaiting_command' ? 'LISTENING' : 'IDLE');
      } catch (err) {
        console.warn('[TARS Voice PTT] Golden-loop error:', err);
        setCompanionState('IDLE');
      }
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

  // Send Chat Message via real backend endpoint with streaming
  const handleSendMessage = async (text: string, inputMode: 'text' | 'voice' = 'text') => {
    cancelAutoHide();
    const convId = activeSessionId;
    const userMsg: TARSAssistantMessage = {
      schema_version: '1.0.0',
      message_id: crypto.randomUUID(),
      conversation_id: convId,
      timestamp: new Date().toISOString(),
      role: 'user',
      content: text,
      input_mode: inputMode,
    };

    console.info('[CHAT] submitted:', text);
    handleIncomingAssistantMessage(userMsg);

    console.info('[CHAT] route selected: assistant query stream');
    setCompanionState('THINKING');
    setStreamingAnswer('');

    let gotFirstDelta = false;
    await assistantClient.streamQuery(text, convId, settings.apiEndpoint, {
      onDelta: (chunk) => {
        if (!gotFirstDelta) {
          gotFirstDelta = true;
          console.info('[CHAT] first delta');
        }
        setStreamingAnswer((prev) => prev + chunk);
      },
      onComplete: (payload) => {
        console.info('[CHAT] complete');
        setStreamingAnswer('');
        handleIncomingAssistantMessage(payload.message!);
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

  // Inspect Setup in Workspace Alerts
  const handleInspectSetup = (setup: TARSTradingEvent) => {
    setSelectedAlert(setup);
    setActiveTab('workspace');
    setWorkspaceSection('alerts');
  };

  // Manual Trigger Mock Event
  const handleManualTriggerMock = () => {
    const evt = createMockTradingEvent();
    handleIncomingTradingEvent(evt);
  };

  // Global Shortcuts for summoning voice panel
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
      if (isTauri()) {
        const { listen } = await import('@tauri-apps/api/event');
        cleanupPtt = await listen('tars://ptt-toggle', () => handleTogglePushToTalk());
      }
    })();

    return () => {
      window.removeEventListener('keydown', handleKeyDown);
      unregisterGlobalShortcut('CommandOrControl+Shift+Space');
      unregisterGlobalShortcut('CommandOrControl+Shift+T');
      if (cleanupPtt) cleanupPtt();
    };
  }, []);

  const sessionMetas = sessions.map((s) => ({
    id: s.id,
    title: s.title,
    createdAt: s.createdAt,
    messageCount: s.messages.length,
  }));

  return (
    <>
      {/* Background Voice Assistant Runtime */}
      <VoiceAssistantRuntime
        visible={appMode === 'voice'}
        onModeChange={setAppMode}
      />

      {/* Main OpenJarvis-Style Desktop Application Shell */}
      {appMode === 'workstation' && (
        <AppShell
          activeTab={activeTab}
          setActiveTab={setActiveTab}
          workspaceSection={workspaceSection}
          onSelectWorkspaceSection={setWorkspaceSection}
          sessions={sessionMetas}
          activeSessionId={activeSessionId}
          onSelectSession={setActiveSessionId}
          onNewChat={handleNewChat}
          onDeleteSession={handleDeleteSession}
          onClearHistory={handleClearHistory}
          companionState={companionState}
          connectionStatus={connectionState.status}
        >
          {activeTab === 'tars' && (
            <ConversationView
              messages={chatMessages}
              streamingAnswer={streamingAnswer}
              analysisProgress={analysisProgress}
              companionState={companionState}
              isListening={isListening}
              onTogglePushToTalk={handleTogglePushToTalk}
              onSendMessage={handleSendMessage}
              onOpenWorkspace={() => setActiveTab('workspace')}
              onSpeak={(text) => {
                audioService.speakText(text, settings.speechRate, settings.speechVolume);
              }}
            />
          )}

          {activeTab === 'workspace' && (
            <WorkspaceView
              section={workspaceSection}
              setSection={setWorkspaceSection}
              connectionState={connectionState}
              activeSetups={activeSetups}
              alertsHistory={alertsHistory}
              selectedAlert={selectedAlert}
              setSelectedAlert={setSelectedAlert}
              isListening={isListening}
              onTogglePushToTalk={handleTogglePushToTalk}
              audioVolume={audioVolume}
              onSendMessage={handleSendMessage}
              onInspectSetup={handleInspectSetup}
              apiEndpoint={settings.apiEndpoint}
              mockModeActive={settings.mockGeneratorActive}
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
        </AppShell>
      )}
    </>
  );
};
