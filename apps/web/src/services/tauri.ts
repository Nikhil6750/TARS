/**
 * Tauri 2 Native Integration Bridge
 * Gracefully provides native desktop capabilities with full browser/PWA fallback.
 */

export function isTauri(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

export interface TauriPlatformInfo {
  isNative: boolean;
  platform: 'windows' | 'macos' | 'linux' | 'ios' | 'android' | 'web';
}

export function detectPlatform(): TauriPlatformInfo {
  if (isTauri()) {
    return { isNative: true, platform: 'windows' };
  }

  const userAgent = typeof navigator !== 'undefined' ? navigator.userAgent : '';
  if (/iPhone|iPad|iPod/i.test(userAgent)) {
    return { isNative: false, platform: 'ios' };
  }
  if (/Android/i.test(userAgent)) {
    return { isNative: false, platform: 'android' };
  }
  if (/Windows/i.test(userAgent)) {
    return { isNative: false, platform: 'windows' };
  }
  if (/Macintosh/i.test(userAgent)) {
    return { isNative: false, platform: 'macos' };
  }
  return { isNative: false, platform: 'web' };
}

export async function toggleCompactWindow(isCompact: boolean): Promise<void> {
  if (!isTauri()) {
    console.info(`[Tauri Mock] Toggled compact window mode: ${isCompact}`);
    return;
  }

  try {
    const { getCurrentWindow, LogicalSize } = await import('@tauri-apps/api/window');
    const appWindow = getCurrentWindow();
    if (isCompact) {
      await appWindow.setSize(new LogicalSize(420, 720));
      await appWindow.setAlwaysOnTop(true);
    } else {
      await appWindow.setSize(new LogicalSize(1280, 840));
      await appWindow.setAlwaysOnTop(false);
    }
  } catch (err) {
    console.warn('Failed to resize window in Tauri:', err);
  }
}

export async function minimizeWindow(): Promise<void> {
  if (isTauri()) {
    try {
      const { getCurrentWindow } = await import('@tauri-apps/api/window');
      await getCurrentWindow().minimize();
    } catch (e) {
      console.warn('Tauri minimize error:', e);
    }
  }
}

export async function toggleMaximizeWindow(): Promise<void> {
  if (isTauri()) {
    try {
      const { getCurrentWindow } = await import('@tauri-apps/api/window');
      await getCurrentWindow().toggleMaximize();
    } catch (e) {
      console.warn('Tauri maximize error:', e);
    }
  }
}

export async function closeWindow(): Promise<void> {
  if (isTauri()) {
    try {
      const { getCurrentWindow } = await import('@tauri-apps/api/window');
      await getCurrentWindow().close();
    } catch (e) {
      console.warn('Tauri close error:', e);
    }
  }
}
