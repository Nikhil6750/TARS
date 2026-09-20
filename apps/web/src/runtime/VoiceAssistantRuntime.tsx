import React, { useEffect, useMemo, useRef } from 'react';
import { OrbCompanion, OrbActions } from '../components/orb/OrbCompanion';
import { orbStore } from '../orb/orbStore';
import { orbNative } from '../orb/orbNative';
import { ALERT_EVENT, TarsAlert } from '../services/monitors';
import { isTauri } from '../services/tauri';
import { realtimeVoiceClient } from './RealtimeVoiceClient';
import { windowLifecycle } from './WindowLifecycle';

interface VoiceAssistantRuntimeProps {
  /** Orb (compact) presence is showing. The runtime itself stays mounted in every mode. */
  visible: boolean;
  onModeChange: (mode: 'voice' | 'workstation') => void;
  onOpenSection?: (section: 'settings') => void;
}

/**
 * Bridges the ONE realtime voice session to the orb. It owns no conversational state:
 * every backend event is forwarded to orbStore, which maps them to the orb's visual state.
 */
export const VoiceAssistantRuntime: React.FC<VoiceAssistantRuntimeProps> = ({ visible, onModeChange, onOpenSection }) => {
  const modeRef = useRef(onModeChange);
  modeRef.current = onModeChange;
  const sectionRef = useRef(onOpenSection);
  sectionRef.current = onOpenSection;

  useEffect(() => {
    void windowLifecycle.start(mode => modeRef.current(
      mode === 'full' || mode === 'workstation' ? 'workstation' : 'voice'
    ));
    void realtimeVoiceClient.start(event => orbStore.apply(event), () => undefined);
    void orbNative.restorePosition();
    return () => { realtimeVoiceClient.stop(); windowLifecycle.stop(); };
  }, []);

  // Proactive alerts: the orb pulses and shows a tiny bubble. The workspace is NOT opened.
  useEffect(() => {
    const onAlert = (e: Event) => {
      const alert = (e as CustomEvent<TarsAlert>).detail;
      const plain = (alert.analysis ?? '').replace(/[*_`#>]+/g, '').replace(/\s+/g, ' ').trim();
      orbStore.alert(plain ? plain.slice(0, 120) : alert.title, alert.replay);
    };
    window.addEventListener(ALERT_EVENT, onAlert);
    return () => window.removeEventListener(ALERT_EVENT, onAlert);
  }, []);

  useEffect(() => {
    if (!isTauri()) return;
    void import('@tauri-apps/api/core').then(async ({ invoke }) => {
      const status = await invoke<{ shortcut: string; registered: boolean }[]>('get_hotkey_status');
      const failed = status.filter(item => !item.registered);
      if (failed.length) orbStore.alert(`${failed.map(item => item.shortcut).join(', ')} unavailable; use the tray icon`);
    }).catch(() => undefined);
  }, []);

  const actions: OrbActions = useMemo(() => ({
    wake: () => realtimeVoiceClient.wake(),
    setMuted: (muted: boolean) => realtimeVoiceClient.setMuted(muted),
    confirm: (approve: boolean) => realtimeVoiceClient.confirmAction(approve),
    openWorkspace: (section) => {
      void orbNative.expand();
      if (section) sectionRef.current?.(section);
    },
    quit: () => void orbNative.quit(),
  }), []);

  if (!visible) return null;
  return <OrbCompanion actions={actions} />;
};
