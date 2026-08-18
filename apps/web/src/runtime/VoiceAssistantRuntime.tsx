import React, { useCallback, useEffect, useRef, useState } from 'react';
import { VoicePanel, toVoicePanelStatus } from '../components/voice/VoicePanel';
import { audioService } from '../services/audio';
import { nativeBridge } from '../services/native-bridge';
import { CompanionVisualState } from '../types/companion';
import { wakeClient } from './WakeClient';
import { windowLifecycle, SummonMode } from './WindowLifecycle';
import { assistantClient } from './AssistantClient';
import { chartAnalysisClient } from './ChartAnalysisClient';

const ANALYZE_CHART_REGEX =
  /\b(analy[sz]e|check|look\s+at|evaluate|read|scan|inspect|review|what\s+do\s+you\s+see\s+on)[\s,]+(?:this|the|my|active|current)?\s*charts?\b/i;

const AUTO_HIDE_MS = 2800;

interface VoiceAssistantRuntimeProps {
  apiEndpoint: string;
  visible: boolean;
  onModeChange: (mode: 'voice' | 'workstation') => void;
}

/**
 * Owns the entire "Hey TARS" voice-first interaction loop: native wake
 * events in, streamed assistant/chart replies out, sentence-by-sentence
 * TTS in between. Always mounted (regardless of `visible`) so wake
 * detection keeps working even while the optional workstation dashboard is
 * on screen -- see WakeClient's doc comment for why that matters.
 */
export const VoiceAssistantRuntime: React.FC<VoiceAssistantRuntimeProps> = ({
  apiEndpoint,
  visible,
  onModeChange,
}) => {
  const [status, setStatus] = useState<CompanionVisualState>('IDLE');
  const [transcript, setTranscript] = useState('');
  const [streamedAnswer, setStreamedAnswer] = useState('');
  const [audioVolume, setAudioVolume] = useState(0);

  const statusRef = useRef(status);
  statusRef.current = status;
  const conversationIdRef = useRef<string>(crypto.randomUUID());
  const ttsQueueRef = useRef<string[]>([]);
  const ttsDrainingRef = useRef(false);
  const pendingSentenceRef = useRef('');
  const autoHideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const cancelAutoHide = useCallback(() => {
    if (autoHideTimerRef.current !== null) {
      clearTimeout(autoHideTimerRef.current);
      autoHideTimerRef.current = null;
    }
  }, []);

  const scheduleAutoHide = useCallback(() => {
    cancelAutoHide();
    autoHideTimerRef.current = setTimeout(() => {
      void windowLifecycle.hide();
    }, AUTO_HIDE_MS);
  }, [cancelAutoHide]);

  const drainTtsQueue = useCallback(async () => {
    if (ttsDrainingRef.current) return;
    ttsDrainingRef.current = true;
    try {
      while (ttsQueueRef.current.length > 0) {
        const next = ttsQueueRef.current.shift();
        if (!next) continue;
        setStatus('SPEAKING');
        try {
          await audioService.synthesizeAndPlay(next, apiEndpoint, setAudioVolume);
        } catch (err) {
          console.warn('[VoiceAssistantRuntime] TTS playback failed:', err);
        }
      }
    } finally {
      ttsDrainingRef.current = false;
      setAudioVolume(0);
    }
  }, [apiEndpoint]);

  const enqueueSentence = useCallback(
    (sentence: string) => {
      const trimmed = sentence.trim();
      if (!trimmed) return;
      ttsQueueRef.current.push(trimmed);
      void drainTtsQueue();
    },
    [drainTtsQueue]
  );

  /** Feeds one streamed text chunk in; enqueues each sentence for TTS as soon as it completes, without waiting for the full reply. */
  const feedStreamingText = useCallback(
    (chunk: string) => {
      pendingSentenceRef.current += chunk;
      const matches = pendingSentenceRef.current.match(/[^.!?]*[.!?]+\s*/g);
      if (matches) {
        const consumedLen = matches.join('').length;
        pendingSentenceRef.current = pendingSentenceRef.current.slice(consumedLen);
        for (const sentence of matches) {
          enqueueSentence(sentence);
        }
      }
    },
    [enqueueSentence]
  );

  const flushPendingSentence = useCallback(() => {
    if (pendingSentenceRef.current.trim()) {
      enqueueSentence(pendingSentenceRef.current);
    }
    pendingSentenceRef.current = '';
  }, [enqueueSentence]);

  const finishTurn = useCallback(() => {
    if (statusRef.current !== 'SPEAKING') {
      setStatus('IDLE');
    }
    scheduleAutoHide();
  }, [scheduleAutoHide]);

  const runAssistantQuery = useCallback(
    async (text: string) => {
      cancelAutoHide();
      setTranscript(text);
      setStreamedAnswer('');
      setStatus('THINKING');
      pendingSentenceRef.current = '';

      await assistantClient.streamQuery(text, conversationIdRef.current, apiEndpoint, {
        onDelta: (chunk) => {
          setStreamedAnswer((prev) => prev + chunk);
          feedStreamingText(chunk);
        },
        onComplete: () => {
          flushPendingSentence();
          void drainTtsQueue().then(finishTurn);
        },
        onError: (detail) => {
          setStreamedAnswer(`I couldn't reach the assistant: ${detail}`);
          setStatus('IDLE');
          scheduleAutoHide();
        },
      });
    },
    [apiEndpoint, cancelAutoHide, drainTtsQueue, feedStreamingText, finishTurn, flushPendingSentence, scheduleAutoHide]
  );

  const runChartAnalysis = useCallback(
    async (triggerPhrase: string) => {
      cancelAutoHide();
      setTranscript(triggerPhrase);
      setStreamedAnswer('Looking at the chart...');
      setStatus('THINKING');
      pendingSentenceRef.current = '';

      await chartAnalysisClient.analyze(apiEndpoint, conversationIdRef.current, {
        onStatus: (text) => setStreamedAnswer(text),
        onDelta: (chunk) => setStreamedAnswer((prev) => (prev === 'Looking at the chart...' ? chunk : prev + chunk)),
        onComplete: (result, timing) => {
          console.info('[VoiceAssistantRuntime] chart analysis timing (ms):', timing);
          setStreamedAnswer(result.formatted_tars_text || result.market_context);
          if (result.speech_text) enqueueSentence(result.speech_text);
          void drainTtsQueue().then(finishTurn);
        },
        onError: (detail) => {
          setStreamedAnswer(`Chart analysis failed: ${detail}`);
          setStatus('IDLE');
          scheduleAutoHide();
        },
      });
    },
    [apiEndpoint, cancelAutoHide, drainTtsQueue, enqueueSentence, finishTurn, scheduleAutoHide]
  );

  const handleBargeIn = useCallback(() => {
    if (statusRef.current !== 'SPEAKING') return;
    ttsQueueRef.current = [];
    pendingSentenceRef.current = '';
    audioService.stopSpeaking();
    void wakeClient.forceCommandCapture();
    setStatus('LISTENING');
    setStreamedAnswer('');
  }, []);

  useEffect(() => {
    const onSummon = (mode: SummonMode) => {
      onModeChange(mode === 'full' || mode === 'workstation' ? 'workstation' : 'voice');
    };
    void windowLifecycle.start(onSummon);

    void wakeClient.start({
      onWakeDetected: () => {
        void nativeBridge.summonHUD('voice');
        cancelAutoHide();
        setTranscript('');
        setStreamedAnswer('');
        setStatus('LISTENING');
      },
      onAnalyzeChartDetected: (phrase) => {
        void nativeBridge.summonHUD('voice');
        void runChartAnalysis(phrase);
      },
      onCommandTranscript: (text) => {
        void nativeBridge.summonHUD('voice');
        if (ANALYZE_CHART_REGEX.test(text)) {
          void runChartAnalysis(text);
        } else {
          void runAssistantQuery(text);
        }
      },
      onCommandTimeout: () => {
        if (statusRef.current === 'LISTENING') {
          setStatus('IDLE');
          scheduleAutoHide();
        }
      },
      onSpeechStart: () => handleBargeIn(),
      onAudioLevel: (level) => {
        if (statusRef.current === 'LISTENING') setAudioVolume(level);
      },
    });

    return () => {
      windowLifecycle.stop();
      wakeClient.stop();
      cancelAutoHide();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (!visible) return null;

  return (
    <div className="fixed inset-0 w-screen h-screen">
      <VoicePanel
        status={toVoicePanelStatus(status)}
        audioVolume={audioVolume}
        transcript={transcript}
        streamedAnswer={streamedAnswer}
        onDismiss={() => void windowLifecycle.hide()}
      />
    </div>
  );
};
