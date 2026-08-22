import React, { useCallback, useEffect, useRef, useState } from 'react';
import { VoicePanel, toVoicePanelStatus } from '../components/voice/VoicePanel';
import { audioService } from '../services/audio';
import { nativeBridge } from '../services/native-bridge';
import { AssistantResponse } from '../types/assistant-response';
import { CompanionVisualState } from '../types/companion';
import { wakeClient } from './WakeClient';
import { SummonMode, windowLifecycle } from './WindowLifecycle';

const AUTO_HIDE_MS = 2800;

interface VoiceAssistantRuntimeProps {
  visible: boolean;
  onModeChange: (mode: 'voice' | 'workstation') => void;
}

/**
 * Read-only client of the backend-owned voice turn. Native code supplies one
 * segmented utterance; this component only renders state and plays the audio
 * chunks already synthesized by the backend.
 */
export const VoiceAssistantRuntime: React.FC<VoiceAssistantRuntimeProps> = ({
  visible,
  onModeChange,
}) => {
  const [status, setStatus] = useState<CompanionVisualState>('IDLE');
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

  if (!visible) return null;
  return (
    <div className="fixed inset-0 h-screen w-screen">
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
