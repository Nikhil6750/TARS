/**
 * Thin frontend listener for the native background wake engine
 * (src-tauri/src/wake_engine.rs). Owns no microphone or VAD logic itself --
 * it only subscribes to Tauri events the native runtime already emits, so
 * the panel can render state without ever taking mic ownership. Stopping
 * this listener (e.g. component unmount) does NOT stop wake detection --
 * the native engine keeps listening independently, by design.
 */
import { isTauri } from '../services/tauri';

export interface WakeClientCallbacks {
  onWakeDetected?: (phrase: string) => void;
  onAnalyzeChartDetected?: (phrase: string) => void;
  onCommandTranscript?: (text: string) => void;
  onCommandTimeout?: () => void;
  onSpeechStart?: () => void;
  onAudioLevel?: (level: number) => void;
}

type UnlistenFn = () => void;

export class WakeClient {
  private unlisten: UnlistenFn[] = [];

  public async start(callbacks: WakeClientCallbacks): Promise<boolean> {
    if (!isTauri()) {
      console.info('[WakeClient] Native wake engine unavailable outside Tauri (web preview) -- push-to-talk only.');
      return false;
    }

    const { listen } = await import('@tauri-apps/api/event');
    const subs: Array<Promise<UnlistenFn>> = [];

    if (callbacks.onWakeDetected) {
      subs.push(
        listen<{ text: string }>('tars://wake-detected', (e) => callbacks.onWakeDetected!(e.payload.text))
      );
    }
    if (callbacks.onAnalyzeChartDetected) {
      subs.push(
        listen<{ text: string }>('tars://analyze-chart-detected', (e) =>
          callbacks.onAnalyzeChartDetected!(e.payload.text)
        )
      );
    }
    if (callbacks.onCommandTranscript) {
      subs.push(
        listen<{ text: string }>('tars://command-transcript', (e) => callbacks.onCommandTranscript!(e.payload.text))
      );
    }
    if (callbacks.onCommandTimeout) {
      subs.push(listen('tars://command-timeout', () => callbacks.onCommandTimeout!()));
    }
    if (callbacks.onSpeechStart) {
      subs.push(listen('tars://speech-start', () => callbacks.onSpeechStart!()));
    }
    if (callbacks.onAudioLevel) {
      subs.push(listen<number>('tars://wake-audio-level', (e) => callbacks.onAudioLevel!(e.payload)));
    }

    this.unlisten = await Promise.all(subs);
    return true;
  }

  public stop(): void {
    this.unlisten.forEach((fn) => fn());
    this.unlisten = [];
  }

  /**
   * Forces the native engine into one-shot command-capture mode right now
   * -- used for barge-in: the instant `speech-start` fires while TARS is
   * speaking, call this so the utterance already forming is captured as
   * the next command instead of being wake-phrase-filtered.
   */
  public async forceCommandCapture(): Promise<void> {
    if (!isTauri()) return;
    try {
      const { invoke } = await import('@tauri-apps/api/core');
      await invoke('force_wake_command_capture');
    } catch (err) {
      console.warn('[WakeClient] force_wake_command_capture failed:', err);
    }
  }

  public async status(): Promise<{ running: boolean; last_error: string | null } | null> {
    if (!isTauri()) return null;
    try {
      const { invoke } = await import('@tauri-apps/api/core');
      return await invoke('wake_engine_status');
    } catch {
      return null;
    }
  }
}

export const wakeClient = new WakeClient();
