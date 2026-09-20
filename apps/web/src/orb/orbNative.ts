import { isTauri } from '../services/tauri';
import { nativeBridge } from '../services/native-bridge';

/** Thin wrappers over the existing Tauri commands for the orb window (no second window framework). */
const POS_KEY = 'tars.orb.position';

async function invoke<T = unknown>(cmd: string, args?: Record<string, unknown>): Promise<T | undefined> {
  if (!isTauri()) return undefined;
  try {
    const { invoke: call } = await import('@tauri-apps/api/core');
    return await call<T>(cmd, args);
  } catch (err) {
    console.warn(`[orb] ${cmd} failed`, err);
    return undefined;
  }
}

export const orbNative = {
  startDrag: () => invoke('orb_start_drag'),
  setHitRegions: (regions: Array<[number, number, number, number]>) => invoke('orb_set_hit_regions', { regions }),
  /** Persist the orb position (localStorage survives restarts; Rust also keeps it for collapse/expand). */
  async savePosition() {
    const pos = await invoke<[number, number]>('orb_get_position');
    if (pos) {
      try { localStorage.setItem(POS_KEY, JSON.stringify(pos)); } catch { /* storage unavailable */ }
    }
  },
  /** Restore the remembered position on launch; Rust clamps it onto a visible monitor. */
  async restorePosition() {
    let saved: [number, number] | null = null;
    try {
      const raw = localStorage.getItem(POS_KEY);
      saved = raw ? (JSON.parse(raw) as [number, number]) : null;
    } catch { saved = null; }
    if (saved && Number.isFinite(saved[0]) && Number.isFinite(saved[1])) {
      await invoke('orb_set_saved_position', { x: saved[0], y: saved[1] });
      await nativeBridge.summonHUD('voice');
    }
  },
  expand: () => nativeBridge.summonHUD('workstation'),
  collapse: () => nativeBridge.summonHUD('voice'),
  quit: () => nativeBridge.exitApp(),
};
