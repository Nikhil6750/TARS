import React, { useCallback, useEffect, useRef, useState } from 'react';
import { VoicePanel, toVoicePanelStatus } from '../components/voice/VoicePanel';
import { audioService } from '../services/audio';
import { nativeBridge } from '../services/native-bridge';
import { composeSpeech } from '../services/speech';
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
  const turnAbortRef = useRef<AbortController | null>(null);

  /** Cancels whatever assistant/chart-analysis stream is currently in
   * flight and returns a fresh controller for the next one -- without this,
   * a barge-in only stops audio playback while the interrupted turn's
   * stream keeps running in the background and re-queues its own TTS
   * behind the new turn's, so the two answers end up talking over each
   * other. */
  const beginNewTurn = useCallback((): AbortController => {
    turnAbortRef.current?.abort();
    const controller = new AbortController();
    turnAbortRef.current = controller;
    return controller;
  }, []);

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
        void wakeClient.setPlaybackSpeaking(true);
        try {
          await audioService.synthesizeAndPlay(next, apiEndpoint, setAudioVolume);
        } catch (err) {
          console.warn('[VoiceAssistantRuntime] TTS playback failed:', err);
        }
      }
    } finally {
      ttsDrainingRef.current = false;
      setAudioVolume(0);
      void wakeClient.setPlaybackSpeaking(false);
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

  /**
   * Accumulates streaming tokens, extracts complete speakable sentences,
   * sanitizes each complete sentence into clean speech (free of Markdown,
   * URLs, and code blocks), and enqueues to TTS.
   */
  const accumulateStreamingSpeech = useCallback(
    (chunk: string) => {
      pendingSentenceRef.current += chunk;
      const matches = pendingSentenceRef.current.match(/[^.!?]*[.!?]+\s*/g);
      if (matches) {
        const consumedLen = matches.join('').length;
        pendingSentenceRef.current = pendingSentenceRef.current.slice(consumedLen);
        for (const rawSentence of matches) {
          const clean = composeSpeech(rawSentence);
          if (clean) {
            enqueueSentence(clean);
          }
        }
      }
    },
    [enqueueSentence]
  );

  const flushPendingSentence = useCallback(() => {
    if (pendingSentenceRef.current.trim()) {
      const clean = composeSpeech(pendingSentenceRef.current);
      if (clean) {
        enqueueSentence(clean);
      }
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
      const controller = beginNewTurn();
      setTranscript(text);
      setStreamedAnswer('');
      setStatus('THINKING');
      pendingSentenceRef.current = '';

      await assistantClient.streamQuery(
        text,
        conversationIdRef.current,
        apiEndpoint,
        {
          onDelta: (chunk) => {
            setStreamedAnswer((prev) => prev + chunk);
            accumulateStreamingSpeech(chunk);
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
        },
        controller.signal
      );
    },
    [apiEndpoint, beginNewTurn, cancelAutoHide, drainTtsQueue, accumulateStreamingSpeech, finishTurn, flushPendingSentence, scheduleAutoHide]
  );

  const runChartAnalysis = useCallback(
    async (triggerPhrase: string) => {
      cancelAutoHide();
      const controller = beginNewTurn();
      setTranscript(triggerPhrase);
      setStreamedAnswer('Looking at the chart...');
      setStatus('THINKING');
      pendingSentenceRef.current = '';

      await chartAnalysisClient.analyze(
        apiEndpoint,
        conversationIdRef.current,
        {
          onStatus: (text) => setStreamedAnswer(text),
          onDelta: (chunk) => setStreamedAnswer((prev) => (prev === 'Looking at the chart...' ? chunk : prev + chunk)),
          onComplete: (result, timing) => {
            console.info('[VoiceAssistantRuntime] chart analysis timing (ms):', timing);
            setStreamedAnswer(result.formatted_tars_text || result.market_context);
            if (result.speech_text) {
              enqueueSentence(result.speech_text);
            } else if (result.formatted_tars_text) {
              const fallback = composeSpeech(result.formatted_tars_text);
              if (fallback) enqueueSentence(fallback);
            }
            void drainTtsQueue().then(finishTurn);
          },
          onError: (detail) => {
            setStreamedAnswer(`Chart analysis failed: ${detail}`);
            setStatus('IDLE');
            scheduleAutoHide();
          },
        },
        controller.signal
      );
    },
    [apiEndpoint, beginNewTurn, cancelAutoHide, drainTtsQueue, enqueueSentence, finishTurn, scheduleAutoHide]
  );

  const handleBargeIn = useCallback(() => {
    if (statusRef.current !== 'SPEAKING') return;
    turnAbortRef.current?.abort();
    ttsQueueRef.current = [];
    pendingSentenceRef.current = '';
    audioService.stopSpeaking();
    void wakeClient.setPlaybackSpeaking(false);
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
      onWakeStateChanged: (telemetry) => {
        if (telemetry.state === 'AUDIO' || telemetry.state === 'COMMAND_LISTENING') {
          if (statusRef.current !== 'SPEAKING' && statusRef.current !== 'THINKING') {
            setStatus('LISTENING');
          }
        } else if (telemetry.state === 'PROCESSING' || telemetry.state === 'TRANSCRIBING') {
          if (statusRef.current !== 'SPEAKING') {
            setStatus('THINKING');
          }
        }
      },
    });

    return () => {
      windowLifecycle.stop();
      wakeClient.stop();
      cancelAutoHide();
      turnAbortRef.current?.abort();
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
