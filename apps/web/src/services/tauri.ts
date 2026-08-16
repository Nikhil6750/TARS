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
    const { invoke } = await import('@tauri-apps/api/core');
    await invoke('toggle_compact_mode', { isCompact });
  } catch (cmdErr) {
    try {
      const { getCurrentWindow, LogicalSize } = await import('@tauri-apps/api/window');
      const appWindow = getCurrentWindow();
      if (isCompact) {
        await appWindow.setAlwaysOnTop(true);
        await appWindow.setSize(new LogicalSize(420, 720));
      } else {
        await appWindow.setAlwaysOnTop(false);
        await appWindow.setSize(new LogicalSize(1280, 840));
      }
    } catch (err) {
      console.warn('Failed to resize window in Tauri:', err);
    }
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

export async function registerGlobalShortcut(
  shortcut: string,
  handler: () => void
): Promise<boolean> {
  if (!isTauri()) {
    console.info(`[Tauri Mock] Registered global shortcut: ${shortcut}`);
    return true;
  }

  try {
    const { register, isRegistered } = await import('@tauri-apps/plugin-global-shortcut');
    const shortcuts = Array.from(new Set([shortcut, 'CommandOrControl+Shift+T', 'Ctrl+Shift+T', 'CmdOrControl+Shift+T']));
    for (const sc of shortcuts) {
      try {
        const alreadyRegistered = await isRegistered(sc);
        if (!alreadyRegistered) {
          await register(sc, (event) => {
            if (!event.state || event.state === 'Pressed') {
              handler();
            }
          });
        }
      } catch (innerErr) {
        console.warn(`[Tauri Shortcut] Could not register variant ${sc}:`, innerErr);
      }
    }
    return true;
  } catch (err) {
    console.warn(`Failed to register global shortcut ${shortcut}:`, err);
    return false;
  }
}

export async function unregisterGlobalShortcut(shortcut: string): Promise<void> {
  if (!isTauri()) return;
  try {
    const { unregister } = await import('@tauri-apps/plugin-global-shortcut');
    const shortcuts = Array.from(new Set([shortcut, 'CommandOrControl+Shift+T', 'Ctrl+Shift+T', 'CmdOrControl+Shift+T']));
    for (const sc of shortcuts) {
      try {
        await unregister(sc);
      } catch {
        // Ignore unregister errors on cleanup
      }
    }
  } catch (err) {
    console.warn(`Failed to unregister global shortcut ${shortcut}:`, err);
  }
}
