/**
 * Windows Native Bridge for TARS Desktop Shell (Tauri 2)
 * Provides access to Windows-wide active foreground window context,
 * autostart management, HUD summoning/positioning, and background tray lifecycle.
 */

import { ActiveWindowContext } from '../types/actions';
import { isTauri } from './tauri';

export interface AutostartInfo {
  enabled: boolean;
  path?: string;
}

export class NativeBridgeService {
  /**
   * Retrieves the current foreground window context using Win32 API.
   * Grounded fallback provided when running in Web/PWA or mock mode.
   */
  public async getActiveWindowContext(): Promise<ActiveWindowContext | null> {
    if (!isTauri()) {
      // In browser/PWA environment, return simulated or browser context
      return {
        executable: 'browser.exe',
        process_id: null,
        window_title: typeof document !== 'undefined' ? document.title || 'TARS Web Shell' : 'TARS Companion',
        window_bounds: {
          x: 0,
          y: 0,
          width: typeof window !== 'undefined' ? window.innerWidth : 1280,
          height: typeof window !== 'undefined' ? window.innerHeight : 840,
        },
        captured_at: new Date().toISOString(),
      };
    }

    try {
      const { invoke } = await import('@tauri-apps/api/core');
      const ctx = await invoke<ActiveWindowContext>('get_active_window_context');
      return ctx;
    } catch (err) {
      console.warn('[NativeBridge] Failed to get active window context via Tauri command:', err);
      return null;
    }
  }

  /**
   * Queries Windows Registry (HKCU\Software\Microsoft\Windows\CurrentVersion\Run)
   * to check if TARS is configured to autostart.
   */
  public async getAutostartStatus(): Promise<boolean> {
    if (!isTauri()) {
      const stored = localStorage.getItem('tars_autostart_mock');
      return stored === 'true';
    }

    try {
      const { invoke } = await import('@tauri-apps/api/core');
      const enabled = await invoke<boolean>('get_autostart_status');
      return !!enabled;
    } catch (err) {
      console.warn('[NativeBridge] Failed to query autostart status:', err);
      return false;
    }
  }

  /**
   * Sets or removes TARS in Windows Registry Run key.
   */
  public async setAutostart(enabled: boolean): Promise<boolean> {
    if (!isTauri()) {
      localStorage.setItem('tars_autostart_mock', enabled ? 'true' : 'false');
      return enabled;
    }

    try {
      const { invoke } = await import('@tauri-apps/api/core');
      const res = await invoke<boolean>('set_autostart', { enabled });
      return res;
    } catch (err) {
      console.warn('[NativeBridge] Failed to set autostart status:', err);
      return false;
    }
  }

  /**
   * Summons the HUD window, restoring visibility, focusing, and bringing to top.
   */
  public async summonHUD(mode?: 'compact' | 'full' | 'pill'): Promise<void> {
    if (!isTauri()) {
      console.info(`[NativeBridge Mock] Summoned HUD in mode: ${mode || 'default'}`);
      return;
    }

    try {
      const { invoke } = await import('@tauri-apps/api/core');
      await invoke('summon_hud', { mode });
    } catch {
      try {
        const { getCurrentWindow } = await import('@tauri-apps/api/window');
        const win = getCurrentWindow();
        await win.show();
        await win.unminimize();
        await win.setFocus();
        await win.setAlwaysOnTop(true);
      } catch (innerErr) {
        console.warn('[NativeBridge] Failed to summon HUD window:', innerErr);
      }
    }
  }

  /**
   * Hides the HUD window to system tray.
   */
  public async hideHUD(): Promise<void> {
    if (!isTauri()) {
      console.info('[NativeBridge Mock] Hide HUD');
      return;
    }

    try {
      const { invoke } = await import('@tauri-apps/api/core');
      await invoke('hide_hud');
    } catch {
      try {
        const { getCurrentWindow } = await import('@tauri-apps/api/window');
        await getCurrentWindow().hide();
      } catch (innerErr) {
        console.warn('[NativeBridge] Failed to hide HUD window:', innerErr);
      }
    }
  }

  /**
   * Toggles HUD visibility
   */
  public async toggleHUD(mode?: 'compact' | 'full' | 'pill'): Promise<boolean> {
    if (!isTauri()) {
      return true;
    }

    try {
      const { invoke } = await import('@tauri-apps/api/core');
      return await invoke<boolean>('toggle_hud', { mode });
    } catch (err) {
      console.warn('[NativeBridge] toggle_hud fallback:', err);
      return false;
    }
  }

  /**
   * Explicitly quits TARS desktop application from tray or UI
   */
  public async exitApp(): Promise<void> {
    if (!isTauri()) {
      console.info('[NativeBridge Mock] Exit App');
      return;
    }

    try {
      const { invoke } = await import('@tauri-apps/api/core');
      await invoke('exit_app');
    } catch {
      try {
        const { getCurrentWindow } = await import('@tauri-apps/api/window');
        await getCurrentWindow().destroy();
      } catch (innerErr) {
        console.warn('[NativeBridge] Failed to destroy window:', innerErr);
      }
    }
  }

  /**
   * Listen to native global events (e.g. Summon shortcut, PTT trigger from OS)
   */
  public async listenToNativeEvents(
    onSummon: () => void,
    onPtt: () => void
  ): Promise<() => void> {
    if (!isTauri()) {
      return () => {};
    }

    try {
      const { listen } = await import('@tauri-apps/api/event');
      const unlistenSummon = await listen('tars://summon-hud', () => onSummon());
      const unlistenPtt = await listen('tars://ptt-toggle', () => onPtt());

      return () => {
        unlistenSummon();
        unlistenPtt();
      };
    } catch (err) {
      console.warn('[NativeBridge] Failed to setup native event listeners:', err);
      return () => {};
    }
  }
}

export const nativeBridge = new NativeBridgeService();
