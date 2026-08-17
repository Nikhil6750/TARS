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
import { HUDOverlay } from './components/hud/HUDOverlay';
import { FloatingVoicePanel } from './components/voice/FloatingVoicePanel';
import { CompanionHero } from './components/companion/CompanionHero';
import { ActiveSetupsView } from './components/setups/ActiveSetupsView';
import { AlertHistoryView } from './components/alerts/AlertHistoryView';
import { AskTARSView } from './components/assistant/AskTARSView';
import { VoiceControlView } from './components/voice/VoiceControlView';
import { MemoryView } from './components/memory/MemoryView';
import { SystemStatusView } from './components/system/SystemStatusView';
import { SettingsView } from './components/settings/SettingsView';
import { nativeBridge } from './services/native-bridge';
import { wakeWordService, WakeWordStatusInfo } from './services/wake-word';
import { ChartAnalysisData } from './components/hud/ChartAnalysisCard';

const ANALYZE_CHART_PATTERN = /\b(analy[sz]e)\s+(this|the|my)?\s*chart\b/i;

export const App: React.FC = () => {
  // App Settings
  const [settings, setSettings] = useState<AppSettings>(loadSettings);

  // View & UI Navigation
  const [activeTab, setActiveTab] = useState<ActiveTab>('companion');
  const [companionState, setCompanionState] = useState<CompanionVisualState>('IDLE');
  const [compactLayoutMode, setCompactLayoutMode] = useState<'voice' | 'hud'>('voice');
  const [activeContextTitle, setActiveContextTitle] = useState<string>('');

  // Real-time Data Stores
  const [activeSetups, setActiveSetups] = useState<TARSTradingEvent[]>([]);
  const [alertsHistory, setAlertsHistory] = useState<TARSTradingEvent[]>(loadStoredAlerts);
  const [selectedAlert, setSelectedAlert] = useState<TARSTradingEvent | null>(null);
  const [chatMessages, setChatMessages] = useState<TARSAssistantMessage[]>(loadStoredChat);
  const [criticalWarnings, setCriticalWarnings] = useState<string[]>([]);
  const [protocolErrors, setProtocolErrors] = useState<Array<{ title: string; errors: string[] }>>([]);

  // Chart Analysis & Demo State
  const [latestChartAnalysis, setLatestChartAnalysis] = useState<ChartAnalysisData | null>(null);
  const [isAnalyzingChart, setIsAnalyzingChart] = useState(false);
  const [streamedAnalysisText, setStreamedAnalysisText] = useState<string>('');
  const [liveTranscript, setLiveTranscript] = useState<string>('');
  const [voiceErrorMessage, setVoiceErrorMessage] = useState<string | null>(null);
  const [wakeStatus, setWakeStatus] = useState<WakeWordStatusInfo>(() => wakeWordService.getStatus());

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
  const companionStateRef = useRef<CompanionVisualState>('IDLE');
  companionStateRef.current = companionState;

  const autoHideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const compactLayoutModeRef = useRef<'voice' | 'hud'>('voice');
  compactLayoutModeRef.current = compactLayoutMode;

  const cancelAutoHide = useCallback(() => {
    if (autoHideTimerRef.current !== null) {
      clearTimeout(autoHideTimerRef.current);
      autoHideTimerRef.current = null;
    }
  }, []);

  const scheduleAutoHide = useCallback((delayMs = 2800) => {
    cancelAutoHide();
    if (settings.compactMode && compactLayoutModeRef.current === 'voice') {
      autoHideTimerRef.current = setTimeout(() => {
        nativeBridge.hideHUD();
      }, delayMs);
    }
  }, [settings.compactMode, cancelAutoHide]);

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

  // Refresh active window context title periodically or on summon
  const refreshContextTitle = useCallback(async () => {
    try {
      const ctx = await nativeBridge.getActiveWindowContext();
      if (ctx) {
        setActiveContextTitle(ctx.window_title || ctx.executable || 'Desktop');
      }
    } catch {
      // ignore
    }
  }, []);

  // Register Global & In-App Shortcuts (Ctrl+Shift+Space / Ctrl+Shift+T / Ctrl+Shift+V)
  useEffect(() => {
    const handleSummonHUD = (mode: 'voice' | 'hud' = 'voice') => {
      cancelAutoHide();
      setSettings((prev) => {
        const next = { ...prev, compactMode: true };
        saveSettings(next);
        return next;
      });
      setCompactLayoutMode(mode);
      nativeBridge.summonHUD(mode);
      refreshContextTitle();
    };

    // 1. In-App Keydown Listener (for web, PWA, and direct in-window input)
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === ' ' || e.code === 'Space')) {
        e.preventDefault();
        handleSummonHUD('voice');
      } else if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === 'T' || e.key === 't')) {
        e.preventDefault();
        handleSummonHUD('voice');
      } else if (e.key === 'Escape') {
        e.preventDefault();
        cancelAutoHide();
        nativeBridge.hideHUD();
      }
    };
    window.addEventListener('keydown', handleKeyDown);

    // 2. Native OS Global Shortcuts
    registerGlobalShortcut('CommandOrControl+Shift+Space', () => handleSummonHUD('voice'));
    registerGlobalShortcut('CommandOrControl+Shift+T', () => handleSummonHUD('voice'));

    // 3. Native event bridge listeners (tars://summon-hud and tars://ptt-toggle)
    let cleanupNativeListeners: (() => void) | undefined;
    nativeBridge.listenToNativeEvents(
      () => handleSummonHUD('voice'),
      () => handleTogglePushToTalk()
    ).then((cleanup) => {
      cleanupNativeListeners = cleanup;
    });

    return () => {
      window.removeEventListener('keydown', handleKeyDown);
      unregisterGlobalShortcut('CommandOrControl+Shift+Space');
      unregisterGlobalShortcut('CommandOrControl+Shift+T');
      if (cleanupNativeListeners) cleanupNativeListeners();
    };
  }, [cancelAutoHide, refreshContextTitle]);

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
      setStreamedAnalysisText(msg.content);

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

  // Barge-in: immediately stops TTS speaking and transitions back to listening
  const handleInterruptSpeech = useCallback(() => {
    cancelAutoHide();
    audioService.stopSpeaking();
    setCompanionState('LISTENING');
    setIsListening(true);
    setAudioVolume(0);
    wakeWordService.beginCommandCaptureManual();
  }, [cancelAutoHide]);

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

    setIsAnalyzingChart(true);
    setCompanionState('THINKING');
    setStreamedAnalysisText('Capturing active screen and analyzing chart...');
    setLatestChartAnalysis(null);

    try {
      const [activeContext, capture] = await Promise.all([
        nativeBridge.getActiveWindowContext(),
        nativeBridge.captureActiveWindow(true),
      ]);

      if (capture.is_secure_desktop) {
        setStreamedAnalysisText('');
        handleIncomingAssistantMessage(
          newMessage(
            "I can't capture the screen right now — a secure desktop or credential prompt is active.",
            'secure_desktop_blocked'
          )
        );
        return;
      }
      if (capture.error) {
        setStreamedAnalysisText('');
        handleIncomingAssistantMessage(
          newMessage(`I couldn't capture the screen to analyze it: ${capture.error}`, capture.error)
        );
        return;
      }

      setStreamedAnalysisText('Chart captured. TARS analyzing price structure...\n\n');

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
          let accumulatedText = '';
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
                  if (event.type === 'delta' && typeof event.text === 'string') {
                    accumulatedText += event.text;
                    setStreamedAnalysisText(accumulatedText);
                  } else if (event.type === 'complete' && event.result) {
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
            setLatestChartAnalysis(finalData);
            setStreamedAnalysisText('');
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
          setStreamedAnalysisText('');
          handleIncomingAssistantMessage(newMessage(`Chart analysis failed: ${detail}`, detail));
          return;
        }

        const result = await response.json();
        setLatestChartAnalysis(result);
        setStreamedAnalysisText('');
        const spoken: string =
          typeof result.speech_text === 'string' && result.speech_text
            ? result.speech_text
            : String(result.market_context || 'No analysis available.');
        handleIncomingAssistantMessage(newMessage(spoken, undefined, result.provider));
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setStreamedAnalysisText('');
      handleIncomingAssistantMessage(newMessage(`Chart analysis error: ${msg}`, msg));
    } finally {
      isAnalyzingChartRef.current = false;
      setIsAnalyzingChart(false);
    }
  }, [settings.apiEndpoint, handleIncomingAssistantMessage, cancelAutoHide]);

  // Shared routing for any already-transcribed voice utterance
  const processVoiceTranscript = useCallback(async (transcript: string) => {
    cancelAutoHide();
    setVoiceErrorMessage(null);
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
      if (activeContext) {
        setActiveContextTitle(activeContext.window_title || activeContext.executable || '');
      }

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
      setVoiceErrorMessage(err instanceof Error ? err.message : String(err));
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

  // Local, always-on background wake listener ("Hey TARS" / "Analyze this chart")
  useEffect(() => {
    const summonVoicePanel = () => {
      cancelAutoHide();
      setSettings((prev) => {
        if (!prev.compactMode) {
          const next = { ...prev, compactMode: true };
          saveSettings(next);
          return next;
        }
        return prev;
      });
      setCompactLayoutMode('voice');
      nativeBridge.summonHUD('voice');
      refreshContextTitle();
    };

    wakeWordService.startListening(
      {
        onWakeDetected: (phrase) => {
          console.info('[TARS Wake] Phrase detected:', phrase);
          summonVoicePanel();
          setCompanionState('WAKE');
          setTimeout(() => {
            setCompanionState('LISTENING');
            setIsListening(true);
          }, 350);
        },
        onAnalyzeChartDetected: async (phrase) => {
          console.info('[TARS Wake] Direct chart analysis command detected:', phrase);
          summonVoicePanel();
          await handleAnalyzeChart();
        },
        onSpeechStart: () => {
          // Real-time barge-in detection: if speaking when user speaks, interrupt immediately!
          if (companionStateRef.current === 'SPEAKING') {
            console.info('[TARS Voice] Barge-in speech onset detected: interrupting TTS');
            audioService.stopSpeaking();
            setCompanionState('LISTENING');
            setIsListening(true);
          }
        },
        onCommandCaptured: async (transcript) => {
          console.info('[TARS Wake] Command captured:', transcript);
          setIsListening(false);
          setAudioVolume(0);
          await processVoiceTranscript(transcript);
        },
        onCommandTimeout: () => {
          console.info('[TARS Wake] No command heard after wake, returning to background listening.');
          setIsListening(false);
          setAudioVolume(0);
          setCompanionState('IDLE');
          scheduleAutoHide(2000);
        },
        onTranscriptInterim: (text) => {
          setLiveTranscript(text);
        },
        onStateChange: (status) => {
          setWakeStatus(status);
        },
        onAudioLevel: (level) => {
          setAudioVolume(level);
        },
      },
      settings.apiEndpoint
    );

    return () => {
      wakeWordService.stopListening();
    };
  }, [handleAnalyzeChart, processVoiceTranscript, settings.apiEndpoint, cancelAutoHide, scheduleAutoHide, refreshContextTitle]);

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

  // Send Chat Message via real backend endpoint
  const handleSendMessage = async (text: string, inputMode: 'text' | 'voice' = 'text') => {
    cancelAutoHide();
    const convId = 'conv_main_session';
    const userMsg: TARSAssistantMessage = {
      schema_version: '1.0.0',
      message_id: crypto.randomUUID ? crypto.randomUUID() : 'msg_' + Date.now(),
      conversation_id: convId,
      timestamp: new Date().toISOString(),
      role: 'user',
      content: text,
      input_mode: inputMode,
    };

    handleIncomingAssistantMessage(userMsg);

    if (ANALYZE_CHART_PATTERN.test(text)) {
      await handleAnalyzeChart();
      return;
    }

    setCompanionState('THINKING');

    try {
      const response = await fetch(`${settings.apiEndpoint}/api/v1/assistant/query`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text,
          conversation_id: convId,
        }),
      });

      if (response.ok) {
        const data = await response.json();
        handleIncomingAssistantMessage(data);
        return;
      }
    } catch (err) {
      console.warn('[TARS Chat API] HTTP Assistant query error, trying WebSocket:', err);
    }

    // Try WebSocket if HTTP was unreachable
    if (wsClientRef.current && wsClientRef.current.getStatus() === 'connected') {
      wsClientRef.current.send({
        type: 'assistant_message',
        payload: userMsg
      });
    } else if (settings.mockGeneratorActive) {
      setTimeout(() => {
        const reply = createMockAssistantReply(text, convId, activeSetups);
        handleIncomingAssistantMessage(reply);
      }, 500);
    } else {
      setCompanionState('IDLE');
      scheduleAutoHide(2500);
    }
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

  // Compact Mode (Minimal Voice Panel or Full HUD)
  if (settings.compactMode) {
    if (compactLayoutMode === 'voice') {
      return (
        <div className="w-screen h-screen bg-transparent p-1 overflow-hidden flex items-center justify-center">
          <FloatingVoicePanel
            companionState={companionState}
            isListening={isListening}
            onTogglePushToTalk={handleTogglePushToTalk}
            audioVolume={audioVolume}
            liveTranscript={liveTranscript}
            streamedText={streamedAnalysisText}
            responseSpeakerText={chatMessages.length > 0 && chatMessages[chatMessages.length - 1].role === 'assistant' ? chatMessages[chatMessages.length - 1].content : undefined}
            errorMessage={voiceErrorMessage}
            wakeStatus={wakeStatus}
            onToggleWakeListening={() => {
              if (wakeStatus.isActive) {
                wakeWordService.stopListening();
                setWakeStatus(wakeWordService.getStatus());
              } else {
                wakeWordService.startListening(
                  {
                    onWakeDetected: () => {
                      setSettings((prev) => ({ ...prev, compactMode: true }));
                      setCompanionState('WAKE');
                      setTimeout(() => {
                        setCompanionState('LISTENING');
                        setIsListening(true);
                      }, 350);
                    },
                    onAnalyzeChartDetected: () => handleAnalyzeChart(),
                    onSpeechStart: () => {
                      if (companionStateRef.current === 'SPEAKING') {
                        audioService.stopSpeaking();
                        setCompanionState('LISTENING');
                        setIsListening(true);
                      }
                    },
                    onCommandCaptured: async (transcript) => {
                      setIsListening(false);
                      setAudioVolume(0);
                      await processVoiceTranscript(transcript);
                    },
                    onCommandTimeout: () => {
                      setIsListening(false);
                      setAudioVolume(0);
                      setCompanionState('IDLE');
                      scheduleAutoHide(2000);
                    },
                    onTranscriptInterim: (text) => setLiveTranscript(text),
                    onStateChange: (status) => setWakeStatus(status),
                    onAudioLevel: (level) => setAudioVolume(level),
                  },
                  settings.apiEndpoint
                );
                setWakeStatus(wakeWordService.getStatus());
              }
            }}
            onInterruptSpeech={handleInterruptSpeech}
            onExpandToHUD={() => {
              setCompactLayoutMode('hud');
              nativeBridge.setWindowSize(440, 740, true);
            }}
            onExpandToWorkstation={() => {
              updateSettings({ compactMode: false });
              nativeBridge.setWindowSize(1280, 840, false);
            }}
            onDismiss={() => {
              cancelAutoHide();
              nativeBridge.hideHUD();
            }}
            activeContextTitle={activeContextTitle}
          />
        </div>
      );
    }

    // HUD Mode (Wave 2A/2B Action & Trading HUD)
    return (
      <div className="w-screen h-screen bg-[#03060a] p-1 overflow-hidden">
        <HUDOverlay
          companionState={companionState}
          onExpand={() => updateSettings({ compactMode: false })}
          onHideHUD={() => {
            cancelAutoHide();
            nativeBridge.hideHUD();
          }}
          activeSetups={activeSetups}
          criticalWarnings={criticalWarnings}
          isListening={isListening}
          onTogglePushToTalk={handleTogglePushToTalk}
          audioVolume={audioVolume}
          apiEndpoint={settings.apiEndpoint}
          onSendMessage={handleSendMessage}
          onAnalyzeChart={handleAnalyzeChart}
          latestChartAnalysis={latestChartAnalysis}
          onClearChartAnalysis={() => setLatestChartAnalysis(null)}
          wakeStatus={wakeStatus}
          onToggleWakeListening={() => {
            if (wakeStatus.isActive) {
              wakeWordService.stopListening();
              setWakeStatus(wakeWordService.getStatus());
            } else {
              wakeWordService.startListening(
                {
                  onWakeDetected: () => {
                    setSettings((prev) => ({ ...prev, compactMode: true }));
                    setCompanionState('WAKE');
                    setTimeout(() => {
                      setCompanionState('LISTENING');
                      setIsListening(true);
                    }, 350);
                  },
                  onAnalyzeChartDetected: () => handleAnalyzeChart(),
                  onSpeechStart: () => {
                    if (companionStateRef.current === 'SPEAKING') {
                      audioService.stopSpeaking();
                      setCompanionState('LISTENING');
                      setIsListening(true);
                    }
                  },
                  onCommandCaptured: async (transcript) => {
                    setIsListening(false);
                    setAudioVolume(0);
                    await processVoiceTranscript(transcript);
                  },
                  onCommandTimeout: () => {
                    setIsListening(false);
                    setAudioVolume(0);
                    setCompanionState('IDLE');
                  },
                  onTranscriptInterim: (text) => setLiveTranscript(text),
                  onStateChange: (status) => setWakeStatus(status),
                  onAudioLevel: (level) => setAudioVolume(level),
                },
                settings.apiEndpoint
              );
              setWakeStatus(wakeWordService.getStatus());
            }
          }}
          liveTranscript={liveTranscript}
          isAnalyzingChart={isAnalyzingChart}
          streamedAnalysisText={streamedAnalysisText}
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
        setCompactMode={(compact) => {
          updateSettings({ compactMode: compact });
          if (compact) {
            setCompactLayoutMode('voice');
            nativeBridge.summonHUD('voice');
          }
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
  );
};
