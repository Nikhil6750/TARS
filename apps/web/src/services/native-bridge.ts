/**
 * Windows Native Bridge for TARS Desktop Shell (Tauri 2)
 * Provides access to Windows-wide active foreground window context,
 * screen awareness (monitor geometry, active window & region capture),
 * UI element tree inspection, autostart, and background tray lifecycle.
 */

import {
  ActiveWindowContext,
  MonitorInfo,
  ScreenCaptureResult,
  UIElementNode,
} from '../types/actions';
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
   * Retrieves monitor geometry for all connected displays (DPI-aware bounds & work areas).
   */
  public async getMonitorsGeometry(): Promise<MonitorInfo[]> {
    if (!isTauri()) {
      const w = typeof window !== 'undefined' ? window.innerWidth : 1920;
      const h = typeof window !== 'undefined' ? window.innerHeight : 1080;
      const dpr = typeof window !== 'undefined' ? window.devicePixelRatio || 1.0 : 1.0;
      return [
        {
          id: 'DISPLAY_PRIMARY_WEB',
          name: 'Web Viewport Display',
          is_primary: true,
          bounds: { x: 0, y: 0, width: w, height: h },
          work_area: { x: 0, y: 0, width: w, height: h },
          scale_factor: dpr,
          dpi: Math.round(dpr * 96),
        },
      ];
    }

    try {
      const { invoke } = await import('@tauri-apps/api/core');
      const monitors = await invoke<MonitorInfo[]>('get_monitors_geometry');
      return monitors;
    } catch (err) {
      console.warn('[NativeBridge] Failed to get monitor geometry:', err);
      return [];
    }
  }

  /**
   * Captures the active foreground window on explicit request.
   * Enforces secure desktop protection (never captures UAC or lock screen).
   */
  public async captureActiveWindow(includeImageData: boolean = true): Promise<ScreenCaptureResult> {
    if (!isTauri()) {
      const now = new Date().toISOString();
      return {
        capture_id: `cap_web_${Date.now()}`,
        captured_at: now,
        source: 'active_window',
        executable: 'browser.exe',
        window_title: typeof document !== 'undefined' ? document.title || 'Web Workspace' : 'Web Context',
        bounds: {
          x: 0,
          y: 0,
          width: typeof window !== 'undefined' ? window.innerWidth : 1280,
          height: typeof window !== 'undefined' ? window.innerHeight : 840,
        },
        scale_factor: typeof window !== 'undefined' ? window.devicePixelRatio || 1.0 : 1.0,
        dpi: typeof window !== 'undefined' ? Math.round((window.devicePixelRatio || 1.0) * 96) : 96,
        width: typeof window !== 'undefined' ? window.innerWidth : 1280,
        height: typeof window !== 'undefined' ? window.innerHeight : 840,
        is_secure_desktop: false,
        image_format: 'image/bmp',
        image_data_base64: 'data:image/bmp;base64,Qk0AAAAAAAAAAAAAAA==',
        temp_file_path: null,
        error: null,
      };
    }

    try {
      const { invoke } = await import('@tauri-apps/api/core');
      return await invoke<ScreenCaptureResult>('capture_active_window', { includeImageData });
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : String(err);
      console.warn('[NativeBridge] Failed to capture active window:', errMsg);
      return {
        capture_id: `cap_err_${Date.now()}`,
        captured_at: new Date().toISOString(),
        source: 'active_window',
        executable: 'unknown.exe',
        window_title: 'Capture Failed',
        bounds: { x: 0, y: 0, width: 0, height: 0 },
        scale_factor: 1.0,
        dpi: 96,
        width: 0,
        height: 0,
        is_secure_desktop: false,
        image_format: 'image/bmp',
        image_data_base64: null,
        temp_file_path: null,
        error: errMsg,
      };
    }
  }

  /**
   * Chart-analysis capture ("Analyze this chart"): hides TARS's own window
   * first, since plain captureActiveWindow() grabs raw on-screen pixels and
   * would otherwise capture TARS itself sitting over the chart it's meant
   * to read. Restores TARS afterward. Use this instead of
   * captureActiveWindow() for any "analyze what's on screen" flow.
   */
  public async captureChartWindow(includeImageData: boolean = true): Promise<ScreenCaptureResult> {
    if (!isTauri()) {
      return this.captureActiveWindow(includeImageData);
    }

    try {
      const { invoke } = await import('@tauri-apps/api/core');
      return await invoke<ScreenCaptureResult>('capture_chart_window', { includeImageData });
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : String(err);
      console.warn('[NativeBridge] Failed to capture chart window:', errMsg);
      return {
        capture_id: `cap_err_${Date.now()}`,
        captured_at: new Date().toISOString(),
        source: 'active_window',
        executable: 'unknown.exe',
        window_title: 'Capture Failed',
        bounds: { x: 0, y: 0, width: 0, height: 0 },
        scale_factor: 1.0,
        dpi: 96,
        width: 0,
        height: 0,
        is_secure_desktop: false,
        image_format: 'image/bmp',
        image_data_base64: null,
        temp_file_path: null,
        error: errMsg,
      };
    }
  }

  /**
   * Captures a bounded screen region (DPI-aware rectangle).
   */
  public async captureScreenRegion(
    x: number,
    y: number,
    width: number,
    height: number,
    includeImageData: boolean = true
  ): Promise<ScreenCaptureResult> {
    if (!isTauri()) {
      const now = new Date().toISOString();
      return {
        capture_id: `region_web_${Date.now()}`,
        captured_at: now,
        source: 'region',
        executable: 'browser_region',
        window_title: `Region (${x}, ${y}, ${width}x${height})`,
        bounds: { x, y, width, height },
        scale_factor: 1.0,
        dpi: 96,
        width,
        height,
        is_secure_desktop: false,
        image_format: 'image/bmp',
        image_data_base64: 'data:image/bmp;base64,Qk0AAAAAAAAAAAAAAA==',
        temp_file_path: null,
        error: null,
      };
    }

    try {
      const { invoke } = await import('@tauri-apps/api/core');
      return await invoke<ScreenCaptureResult>('capture_screen_region', {
        x: Math.round(x),
        y: Math.round(y),
        width: Math.round(width),
        height: Math.round(height),
        includeImageData,
      });
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : String(err);
      console.warn('[NativeBridge] Failed to capture screen region:', errMsg);
      return {
        capture_id: `region_err_${Date.now()}`,
        captured_at: new Date().toISOString(),
        source: 'region',
        executable: 'unknown.exe',
        window_title: 'Region Capture Failed',
        bounds: { x, y, width, height },
        scale_factor: 1.0,
        dpi: 96,
        width: 0,
        height: 0,
        is_secure_desktop: false,
        image_format: 'image/bmp',
        image_data_base64: null,
        temp_file_path: null,
        error: errMsg,
      };
    }
  }

  /**
   * Retrieves the native Win32 accessibility/UI element hierarchy of the active window.
   */
  public async getActiveWindowElements(): Promise<UIElementNode> {
    if (!isTauri()) {
      return {
        id: 'web_root',
        name: typeof document !== 'undefined' ? document.title || 'Web Document' : 'Web Document',
        role: 'window',
        class_name: 'HTMLDocument',
        bounds: {
          x: 0,
          y: 0,
          width: typeof window !== 'undefined' ? window.innerWidth : 1280,
          height: typeof window !== 'undefined' ? window.innerHeight : 840,
        },
        is_enabled: true,
        is_visible: true,
        children: [
          {
            id: 'web_btn_search',
            name: 'Search Button',
            role: 'button',
            class_name: 'HTMLButtonElement',
            bounds: { x: 50, y: 20, width: 100, height: 35 },
            is_enabled: true,
            is_visible: true,
            children: [],
          },
          {
            id: 'web_input_query',
            name: 'Search Query Input',
            role: 'input',
            class_name: 'HTMLInputElement',
            bounds: { x: 160, y: 20, width: 300, height: 35 },
            is_enabled: true,
            is_visible: true,
            children: [],
          },
        ],
      };
    }

    try {
      const { invoke } = await import('@tauri-apps/api/core');
      return await invoke<UIElementNode>('get_active_window_elements');
    } catch (err) {
      console.warn('[NativeBridge] Failed to get active window elements:', err);
      return {
        id: 'fallback_root',
        name: 'Active Window',
        role: 'window',
        class_name: 'Unknown',
        bounds: null,
        is_enabled: false,
        is_visible: false,
        children: [],
      };
    }
  }

  /**
   * Clears temporary screen captures from local storage/disk.
   */
  public async clearCapturesCache(): Promise<number> {
    if (!isTauri()) {
      return 0;
    }

    try {
      const { invoke } = await import('@tauri-apps/api/core');
      return await invoke<number>('clear_captures_cache');
    } catch (err) {
      console.warn('[NativeBridge] Failed to clear captures cache:', err);
      return 0;
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
   * Sets the native window size and always-on-top mode.
   */
  public async setWindowSize(width: number, height: number, alwaysOnTop?: boolean): Promise<void> {
    if (!isTauri()) {
      return;
    }

    try {
      const { invoke } = await import('@tauri-apps/api/core');
      await invoke('set_window_size', { width, height, alwaysOnTop });
    } catch {
      try {
        const { getCurrentWindow, LogicalSize } = await import('@tauri-apps/api/window');
        const win = getCurrentWindow();
        await win.setSize(new LogicalSize(width, height));
        if (typeof alwaysOnTop === 'boolean') {
          await win.setAlwaysOnTop(alwaysOnTop);
        }
      } catch (innerErr) {
        console.warn('[NativeBridge] Failed to set window size:', innerErr);
      }
    }
  }

  /**
   * Summons the HUD or voice panel window, restoring visibility, focusing, and bringing to top.
   */
  public async summonHUD(mode?: 'voice' | 'compact' | 'hud' | 'full' | 'workstation' | 'pill'): Promise<void> {
    if (!isTauri()) {
      console.info(`[NativeBridge Mock] Summoned HUD in mode: ${mode || 'default'}`);
      return;
    }

    try {
      const { invoke } = await import('@tauri-apps/api/core');
      await invoke('summon_hud', { mode });
    } catch {
      try {
        const { getCurrentWindow, LogicalSize } = await import('@tauri-apps/api/window');
        const win = getCurrentWindow();
        await win.show();
        await win.unminimize();
        if (mode === 'full' || mode === 'workstation') {
          await win.setSize(new LogicalSize(1280, 840));
          await win.setAlwaysOnTop(false);
        } else if (mode === 'hud' || mode === 'compact') {
          await win.setSize(new LogicalSize(440, 740));
          await win.setAlwaysOnTop(true);
        } else {
          // voice mode
          await win.setSize(new LogicalSize(420, 260));
          await win.setAlwaysOnTop(true);
        }
        await win.setFocus();
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
  public async toggleHUD(mode?: 'voice' | 'compact' | 'hud' | 'full' | 'workstation' | 'pill'): Promise<boolean> {
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
