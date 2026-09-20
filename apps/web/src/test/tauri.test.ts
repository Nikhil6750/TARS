import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  isTauri,
  detectPlatform,
  toggleCompactWindow,
  minimizeWindow,
  toggleMaximizeWindow,
  closeWindow
} from '../services/tauri';
import {
  requestNotificationPermission,
  sendNotification
} from '../services/notifications';
import tauriConfig from '../../src-tauri/tauri.conf.json';
import defaultCap from '../../src-tauri/capabilities/default.json';

describe('Tauri 2 Configuration & Service Compatibility', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('validates tauri.conf.json configuration schema alignment', () => {
    expect(tauriConfig.productName).toBe('TARS');
    expect(tauriConfig.identifier).toBe('com.tars.companion');
    expect(tauriConfig.version).toBe('1.0.0');

    // Windows configuration
    expect(tauriConfig.app.windows).toBeInstanceOf(Array);
    expect(tauriConfig.app.windows.length).toBeGreaterThan(0);
    const mainWindow = tauriConfig.app.windows[0];
    expect(mainWindow.label).toBe('main');
    // Native desktop assistant window configuration
    expect(mainWindow.width).toBe(1100);
    expect(mainWindow.height).toBe(780);
    expect(mainWindow.minWidth).toBe(480);
    expect(mainWindow.minHeight).toBe(520);
    expect(mainWindow.visible).toBe(true);

    // Capabilities
    expect(defaultCap.windows).toContain('main');
    expect(defaultCap.permissions).toContain('core:default');
    expect(defaultCap.permissions).toContain('core:window:default');
    expect(defaultCap.permissions).toContain('core:window:allow-set-size');
    expect(defaultCap.permissions).toContain('core:window:allow-set-always-on-top');
    expect(defaultCap.permissions).toContain('notification:default');
    expect(defaultCap.permissions).toContain('global-shortcut:default');

    // Security & CSP
    expect(tauriConfig.app.security.csp).toContain("connect-src 'self' http://127.0.0.1:8000 http://localhost:8000 ws://127.0.0.1:8000 ws://localhost:8000");
    expect(tauriConfig.app.security.csp).not.toContain("unsafe-eval");
  });

  it('detects platform correctly when outside Tauri runtime', () => {
    expect(isTauri()).toBe(false);
    const platform = detectPlatform();
    expect(platform.isNative).toBe(false);
    expect(['windows', 'macos', 'linux', 'ios', 'android', 'web']).toContain(platform.platform);
  });

  it('gracefully handles window actions in browser mode without errors', async () => {
    await expect(toggleCompactWindow(true)).resolves.not.toThrow();
    await expect(toggleCompactWindow(false)).resolves.not.toThrow();
    await expect(minimizeWindow()).resolves.not.toThrow();
    await expect(toggleMaximizeWindow()).resolves.not.toThrow();
    await expect(closeWindow()).resolves.not.toThrow();
  });

  it('handles notification permissions and notification dispatch in browser environment', async () => {
    const granted = await requestNotificationPermission();
    expect(typeof granted).toBe('boolean');

    await expect(
      sendNotification({
        title: 'TARS Validated Setup: XAUUSD',
        body: 'H4 Orderblock tap confirmed. Risk:Reward 2.82.'
      })
    ).resolves.not.toThrow();
  });
});
