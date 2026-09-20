import React, { useEffect, useRef, useState } from 'react';
import { VoicePanel, toVoicePanelStatus } from '../components/voice/VoicePanel';
import { nativeBridge } from '../services/native-bridge';
import { CompanionVisualState } from '../types/companion';
import { realtimeVoiceClient } from './RealtimeVoiceClient';
import { windowLifecycle } from './WindowLifecycle';

interface VoiceAssistantRuntimeProps {
  visible: boolean;
  onModeChange: (mode: 'voice' | 'workstation') => void;
}

/** Render the backend session state. No local conversational state machine. */
export const VoiceAssistantRuntime: React.FC<VoiceAssistantRuntimeProps> = ({ visible, onModeChange }) => {
  const [status, setStatus] = useState<CompanionVisualState>('IDLE');
  const [transcript, setTranscript] = useState('');
  const [answer, setAnswer] = useState('');
  const [volume, setVolume] = useState(0);
  const [error, setError] = useState('');
  const modeRef = useRef(onModeChange);
  modeRef.current = onModeChange;
  useEffect(() => {
    void windowLifecycle.start(mode => modeRef.current(
      mode === 'full' || mode === 'workstation' ? 'workstation' : 'voice'
    ));
    void realtimeVoiceClient.start(event => {
      if (event.type === 'state') {
        const map: Record<string, CompanionVisualState> = {
          IDLE: 'IDLE', LISTENING: 'LISTENING', USER_SPEAKING: 'LISTENING',
          ENDPOINTING: 'LISTENING', THINKING: 'THINKING',
          ASSISTANT_SPEAKING: 'SPEAKING', INTERRUPTING: 'LISTENING', ERROR: 'IDLE',
        };
        setStatus(map[event.state ?? ''] ?? 'IDLE');
        if (event.state === 'ERROR') setError(event.detail ?? 'A voice provider is unavailable.');
        else setError('');
      } else if (event.type === 'speech_started') {
        setTranscript(''); setAnswer('');
        void nativeBridge.summonHUD('voice');
      } else if (event.type === 'partial_transcript' || event.type === 'final_transcript') {
        setTranscript(event.text ?? '');
      } else if (event.type === 'delta') {
        setAnswer(previous => previous + (event.text ?? ''));
      } else if (event.type === 'response_complete' && event.response) {
        setAnswer(event.response.display_text);
      } else if (event.type === 'provider_status' && event.detail) {
        setError(event.detail);
      } else if (event.type === 'metrics') {
        console.info('[TARS voice latency]', event);
      }
    }, setVolume);
    return () => { realtimeVoiceClient.stop(); windowLifecycle.stop(); };
  }, []);
  if (!visible) return null;
  return <div className="fixed inset-0 h-screen w-screen">
    <VoicePanel status={toVoicePanelStatus(status)} audioVolume={volume}
      transcript={transcript} streamedAnswer={error || answer}
      onDismiss={() => void windowLifecycle.hide()} />
  </div>;
};
