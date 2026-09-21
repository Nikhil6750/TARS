import { isTauri } from '../services/tauri';

/**
 * Microphone mute, mirrored from its single authority: the native shell (`MUTED` in wake_engine.rs).
 * The native side persists the boolean, stops emitting audio frames, updates the tray label and emits
 * `tars://mic-muted`. Every control (orb button, tray, Settings) changes it ONLY through `set()`, and
 * every view reads it from here, so they can never disagree.
 */
type Listener = () => void;

class MuteStore {
  private muted = false;
  private listeners = new Set<Listener>();
  private started = false;

  subscribe = (fn: Listener) => {
    this.listeners.add(fn);
    return () => { this.listeners.delete(fn); };
  };

  getSnapshot = () => this.muted;

  /** Apply a value reported by the native authority. */
  apply(value: boolean) {
    if (value === this.muted) return;
    this.muted = value;
    this.listeners.forEach(fn => fn());
  }

  /** Read the persisted native state and follow changes (tray, other windows). Idempotent. */
  async start(): Promise<void> {
    if (this.started || !isTauri()) return;
    this.started = true;
    try {
      const { invoke } = await import('@tauri-apps/api/core');
      const { listen } = await import('@tauri-apps/api/event');
      this.apply(!!(await invoke<boolean>('get_mic_muted')));
      await listen<boolean>('tars://mic-muted', ({ payload }) => this.apply(!!payload));
    } catch (err) {
      console.warn('[mute] could not read native mute state', err);
      this.started = false;
    }
  }

  /** Ask the native authority to change mute; the resulting event updates every view. */
  async set(muted: boolean): Promise<void> {
    if (!isTauri()) {
      this.apply(muted); // web/PWA build has no native owner
      return;
    }
    try {
      const { invoke } = await import('@tauri-apps/api/core');
      await invoke('set_mic_muted', { muted });
    } catch (err) {
      console.warn('[mute] set_mic_muted failed', err);
    }
  }

  toggle(): Promise<void> {
    return this.set(!this.muted);
  }

  /** Test helper. */
  reset() {
    this.muted = false;
    this.started = false;
  }
}

export const muteStore = new MuteStore();
