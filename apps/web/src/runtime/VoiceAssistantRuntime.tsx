import React, { useCallback, useEffect, useRef, useState } from 'react';
import { VoicePanel, toVoicePanelStatus } from '../components/voice/VoicePanel';
import { audioService } from '../services/audio';
import { nativeBridge } from '../services/native-bridge';
import { AssistantResponse } from '../types/assistant-response';
import { CompanionVisualState } from '../types/companion';
import { wakeClient } from './WakeClient';
import { SummonMode, windowLifecycle } from './WindowLifecycle';

const AUTO_HIDE_MS = 2800;
const GEMINI_STATUS_POLL_MS = 750;

interface VoiceAssistantRuntimeProps {
  visible: boolean;
  onModeChange: (mode: 'voice' | 'workstation') => void;
  /** Backend HTTP URL (e.g. http://127.0.0.1:8000), for polling Gemini
   * Live's real connection/listening state -- see GEMINI_STATUS_POLL_MS
   * below. Optional so this component still renders without it. */
  apiEndpoint?: string;
}

interface GeminiLiveStatusResponse {
  enabled: boolean;
  state:
    | 'starting'
    | 'listening'
    | 'user_speaking'
    | 'processing'
    | 'speaking'
    | 'stopped'
    | 'disabled';
  device_name: string | null;
  error: string | null;
  connected: boolean;
  mic_streaming: boolean;
}

/**
 * Read-only client of the backend-owned voice turn. Native code supplies one
 * segmented utterance; this component only renders state and plays the audio
 * chunks already synthesized by the backend.
 */
export const VoiceAssistantRuntime: React.FC<VoiceAssistantRuntimeProps> = ({
  visible,
  onModeChange,
  apiEndpoint,
}) => {
  const [status, setStatus] = useState<CompanionVisualState>('IDLE');
  const [geminiError, setGeminiError] = useState<string | null>(null);
  const [transcript, setTranscript] = useState('');
  const [streamedAnswer, setStreamedAnswer] = useState('');
  const [audioVolume, setAudioVolume] = useState(0);
  const autoHideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const playingRef = useRef(false);
  const onModeChangeRef = useRef(onModeChange);
  onModeChangeRef.current = onModeChange;

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

  const playBackendAudio = useCallback(async (chunks: string[]) => {
    if (chunks.length === 0) return;
    playingRef.current = true;
    setStatus('SPEAKING');
    await wakeClient.setPlaybackSpeaking(true);
    try {
      await audioService.playBase64Chunks(chunks, setAudioVolume);
    } finally {
      playingRef.current = false;
      setAudioVolume(0);
      await wakeClient.setPlaybackSpeaking(false);
    }
  }, []);

  const handleTurnComplete = useCallback(
    async (response: AssistantResponse) => {
      if (response.status === 'ignored') {
        setStatus('IDLE');
        return;
      }

      cancelAutoHide();
      await nativeBridge.summonHUD('voice');
      setTranscript(response.transcript ?? '');
      setStreamedAnswer(response.display_text);
      try {
        await playBackendAudio(response.audio_chunks_base64);
      } catch (error) {
        console.warn('[VoiceAssistantRuntime] backend audio playback failed:', error);
      }

      if (response.status === 'awaiting_command') {
        setStatus('LISTENING');
      } else {
        setStatus('IDLE');
        scheduleAutoHide();
      }
    },
    [cancelAutoHide, playBackendAudio, scheduleAutoHide]
  );

  const geminiActiveRef = useRef(false);

  useEffect(() => {
    const onSummon = (mode: SummonMode) => {
      onModeChangeRef.current(
        mode === 'full' || mode === 'workstation' ? 'workstation' : 'voice'
      );
    };
    void windowLifecycle.start(onSummon);
    void wakeClient.start({
      onAudioLevel: (level) => setAudioVolume(level),
      onWakeStateChanged: ({ state }) => {
        // When Gemini Live owns the microphone, the native Rust wake loop
        // is disabled and never emits these -- but guard explicitly rather
        // than relying on that, so this component never shows a native
        // wake state while a different automatic listener is actually
        // driving the assistant.
        if (geminiActiveRef.current) return;
        if (state === 'SPEECH_DETECTED' || state === 'LISTENING_FOR_COMMAND') {
          setStatus('LISTENING');
        } else if (state === 'TRANSCRIBING' || state === 'PROCESSING') {
          setStatus('THINKING');
        } else if (state === 'SPEAKING') {
          setStatus('SPEAKING');
        } else if (state === 'IDLE' && !playingRef.current) {
          setStatus('IDLE');
        }
      },
      onTurnComplete: (response) => void handleTurnComplete(response),
    });

    return () => {
      windowLifecycle.stop();
      wakeClient.stop();
      cancelAutoHide();
      audioService.stopSpeaking();
    };
  }, [cancelAutoHide, handleTurnComplete]);

  // Polls the ACTUAL backend Gemini Live loop state -- never invents
  // "Listening" locally. Only takes over `status` while Gemini reports
  // itself enabled (the single automatic mic owner in that mode); falls
  // back to the native wake-state-driven flow above otherwise.
  useEffect(() => {
    if (!apiEndpoint) return;
    let cancelled = false;
    const base = apiEndpoint.replace(/\/$/, '');

    const poll = async () => {
      try {
        const res = await fetch(`${base}/api/v1/voice/gemini-status`);
        if (!res.ok || cancelled) return;
        const data = (await res.json()) as GeminiLiveStatusResponse;
        if (cancelled) return;
        geminiActiveRef.current = data.enabled;
        setGeminiError(data.error);
        if (!data.enabled) return;
        // "Listening" must reflect REAL streaming evidence, not just that
        // the turn-phase state machine happens to be in its listening
        // phase -- a connected session whose mic thread has died would
        // otherwise still claim to be listening.
        switch (data.state) {
          case 'starting':
            setStatus('WAKE');
            break;
          case 'listening':
            setStatus(data.connected && data.mic_streaming ? 'LISTENING' : 'WAKE');
            break;
          case 'user_speaking':
            setStatus(data.connected && data.mic_streaming ? 'HEARING' : 'WAKE');
            break;
          case 'processing':
            setStatus('THINKING');
            break;
          case 'speaking':
            if (!playingRef.current) setStatus('SPEAKING');
            break;
          case 'stopped':
            setStatus(data.error ? 'DISCONNECTED' : 'IDLE');
            break;
        }
      } catch {
        // Backend unreachable -- leave whatever status was last known
        // rather than guessing.
      }
    };

    void poll();
    const interval = setInterval(() => void poll(), GEMINI_STATUS_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [apiEndpoint]);

  if (!visible) return null;
  const panelStatus = toVoicePanelStatus(status);
  return (
    <div className="fixed inset-0 h-screen w-screen">
      <VoicePanel
        status={panelStatus}
        audioVolume={audioVolume}
        transcript={transcript}
        streamedAnswer={panelStatus === 'ERROR' && geminiError ? geminiError : streamedAnswer}
        onDismiss={() => void windowLifecycle.hide()}
      />
    </div>
  );
};
