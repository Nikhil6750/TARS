import React, { useEffect, useRef, useState } from 'react';
import { VoicePanel, toVoicePanelStatus } from '../components/voice/VoicePanel';
import { nativeBridge } from '../services/native-bridge';
import { CompanionVisualState } from '../types/companion';
import { ALERT_EVENT, fetchMonitorStatus, MonitorStatus, TarsAlert } from '../services/monitors';
import { realtimeVoiceClient } from './RealtimeVoiceClient';
import { isTauri } from '../services/tauri';
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
  const [stateLabel, setStateLabel] = useState('IDLE');
  const [monitors, setMonitors] = useState<MonitorStatus | null>(null);
  const [alert, setAlert] = useState<TarsAlert | null>(null);
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
        setStateLabel(event.state ?? 'IDLE');
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
  useEffect(() => {
    // Hotkey registration failures are visible, not silent (Rust records each outcome).
    if (!isTauri()) return;
    void import('@tauri-apps/api/core').then(async ({ invoke }) => {
      const status = await invoke<{ shortcut: string; registered: boolean; error?: string }[]>('get_hotkey_status');
      const failed = status.filter(item => !item.registered);
      if (failed.length) {
        setAlert({ title: 'Hotkey unavailable', replay: false, at: Date.now(),
          summary: `${failed.map(item => item.shortcut).join(', ')} could not be registered (in use by another app?). Use the tray icon instead.` });
      }
    }).catch(() => undefined);
  }, []);
  useEffect(() => {
    let alive = true;
    const poll = async () => { const next = await fetchMonitorStatus(); if (alive) setMonitors(next); };
    void poll();
    const timer = window.setInterval(poll, 2000);
    const onAlert = (e: Event) => setAlert((e as CustomEvent<TarsAlert>).detail);
    window.addEventListener(ALERT_EVENT, onAlert);
    return () => { alive = false; window.clearInterval(timer); window.removeEventListener(ALERT_EVENT, onAlert); };
  }, []);
  if (!visible) return null;
  return <div className="fixed inset-0 h-screen w-screen">
    <VoicePanel status={toVoicePanelStatus(status)} audioVolume={volume}
      transcript={transcript} streamedAnswer={error || answer}
      onDismiss={() => void windowLifecycle.hide()}
      stateLabel={stateLabel} monitors={monitors} alert={alert} />
  </div>;
};
