import { describe, it, expect, vi, beforeEach } from 'vitest';
import { NativeBridgeService } from '../services/native-bridge';

describe('Wave 2A NativeBridgeService', () => {
  let bridge: NativeBridgeService;

  beforeEach(() => {
    bridge = new NativeBridgeService();
    vi.restoreAllMocks();
  });

  it('provides grounded active window context in browser fallback mode', async () => {
    const ctx = await bridge.getActiveWindowContext();
    expect(ctx).not.toBeNull();
    expect(ctx?.executable).toBe('browser.exe');
    expect(typeof ctx?.window_title).toBe('string');
    expect(ctx?.window_bounds).toBeDefined();
    expect(ctx?.captured_at).toBeDefined();
  });

  it('manages autostart preference state', async () => {
    const initial = await bridge.getAutostartStatus();
    expect(typeof initial).toBe('boolean');

    const updated = await bridge.setAutostart(true);
    expect(updated).toBe(true);
    expect(await bridge.getAutostartStatus()).toBe(true);

    const disabled = await bridge.setAutostart(false);
    expect(disabled).toBe(false);
    expect(await bridge.getAutostartStatus()).toBe(false);
  });

  it('supports HUD summon, hide, toggle, and exit operations without exceptions', async () => {
    await expect(bridge.summonHUD('compact')).resolves.not.toThrow();
    await expect(bridge.hideHUD()).resolves.not.toThrow();
    await expect(bridge.toggleHUD()).resolves.not.toThrow();
    await expect(bridge.exitApp()).resolves.not.toThrow();
  });

  it('registers and tears down native event listeners', async () => {
    const onSummon = vi.fn();
    const onPtt = vi.fn();

    const cleanup = await bridge.listenToNativeEvents(onSummon, onPtt);
    expect(typeof cleanup).toBe('function');
    cleanup();
  });
});
