/** Thin observer for the native audio transport. It never recognizes wake
 * phrases, routes commands, chooses providers, or creates speech text. */
import { isTauri } from '../services/tauri';
import { AssistantResponse } from '../types/assistant-response';

export type NativeWakeState =
  | 'IDLE'
  | 'SPEECH_DETECTED'
  | 'TRANSCRIBING'
  | 'WAKE_DETECTED'
  | 'LISTENING_FOR_COMMAND'
  | 'PROCESSING'
  | 'SPEAKING';

export interface WakeTimingTelemetry {
  state: NativeWakeState;
  turn_id?: string | null;
  audio_detected_at?: number | null;
  speech_end_at?: number | null;
}

export interface WakeClientCallbacks {
  onAudioLevel?: (level: number) => void;
  onWakeStateChanged?: (telemetry: WakeTimingTelemetry) => void;
  onTurnComplete?: (response: AssistantResponse) => void;
}

type UnlistenFn = () => void;

export class WakeClient {
  private unlisten: UnlistenFn[] = [];

  public async start(callbacks: WakeClientCallbacks): Promise<boolean> {
    if (!isTauri()) return false;

    const { listen } = await import('@tauri-apps/api/event');
    const subscriptions: Array<Promise<UnlistenFn>> = [];
    if (callbacks.onAudioLevel) {
      subscriptions.push(
        listen<number>('tars://wake-audio-level', (event) => callbacks.onAudioLevel?.(event.payload))
      );
    }
    if (callbacks.onWakeStateChanged) {
      subscriptions.push(
        listen<WakeTimingTelemetry>('tars://wake-state-changed', (event) =>
          callbacks.onWakeStateChanged?.(event.payload)
        )
      );
    }
    if (callbacks.onTurnComplete) {
      subscriptions.push(
        listen<AssistantResponse>('tars://assistant-turn-complete', (event) =>
          callbacks.onTurnComplete?.(event.payload)
        )
      );
    }
    this.unlisten = await Promise.all(subscriptions);
    return true;
  }

  public stop(): void {
    this.unlisten.forEach((unlisten) => unlisten());
    this.unlisten = [];
  }

  public async setPlaybackSpeaking(speaking: boolean): Promise<void> {
    if (!isTauri()) return;
    const { invoke } = await import('@tauri-apps/api/core');
    await invoke('set_wake_playback_state', { speaking });
  }

  public async status(): Promise<{ running: boolean; last_error: string | null } | null> {
    if (!isTauri()) return null;
    const { invoke } = await import('@tauri-apps/api/core');
    return invoke('wake_engine_status');
  }
}

export const wakeClient = new WakeClient();
