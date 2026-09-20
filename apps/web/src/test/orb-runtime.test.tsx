import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ALERT_EVENT } from '../services/monitors';

const h = vi.hoisted(() => ({
  summon: null as null | ((mode: string) => void),
  start: vi.fn(),
  stop: vi.fn(),
  expand: vi.fn(),
  collapse: vi.fn(),
  restore: vi.fn(),
  events: null as null | ((e: Record<string, unknown>) => void),
}));

vi.mock('../runtime/WindowLifecycle', () => ({
  windowLifecycle: { start: async (cb: (mode: string) => void) => { h.summon = cb; }, stop: vi.fn() },
}));
vi.mock('../runtime/RealtimeVoiceClient', () => ({
  realtimeVoiceClient: {
    start: async (cb: (e: Record<string, unknown>) => void) => { h.start(); h.events = cb; },
    stop: h.stop, wake: vi.fn(), setMuted: vi.fn(), confirmAction: vi.fn(),
  },
}));
vi.mock('../orb/orbNative', () => ({
  orbNative: { expand: h.expand, collapse: h.collapse, restorePosition: h.restore, quit: vi.fn(), setHitRegions: vi.fn(), startDrag: vi.fn(), savePosition: vi.fn() },
}));

import { VoiceAssistantRuntime } from '../runtime/VoiceAssistantRuntime';
import { orbStore } from '../orb/orbStore';

beforeEach(() => {
  const ctx = new Proxy({ createRadialGradient: () => ({ addColorStop: () => undefined }) } as Record<string, unknown>, {
    get: (t, k: string) => (k in t ? t[k] : () => undefined), set: (t, k: string, v) => { t[k] = v; return true; },
  });
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation((() => ctx) as never);
  vi.stubGlobal('matchMedia', (q: string) => ({ matches: false, media: q, addEventListener: () => undefined, removeEventListener: () => undefined }));
  orbStore.reset();
  vi.clearAllMocks();
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('VoiceAssistantRuntime bridges the single session to the orb', () => {
  it('starts exactly one realtime session and restores the remembered position', async () => {
    render(<VoiceAssistantRuntime visible onModeChange={() => undefined} />);
    await act(async () => { await Promise.resolve(); });
    expect(h.start).toHaveBeenCalledTimes(1);
    expect(h.restore).toHaveBeenCalledTimes(1);
  });

  it('native summon events switch orb <-> workspace (expand / collapse / tray & hotkey summon)', async () => {
    const onMode = vi.fn();
    render(<VoiceAssistantRuntime visible onModeChange={onMode} />);
    await act(async () => { await Promise.resolve(); });
    h.summon?.('workstation');
    h.summon?.('full');
    h.summon?.('voice');
    expect(onMode.mock.calls.map(c => c[0])).toEqual(['workstation', 'workstation', 'voice']);
  });

  it('renders the orb only in compact mode but keeps the session alive in the workspace', async () => {
    const { rerender } = render(<VoiceAssistantRuntime visible onModeChange={() => undefined} />);
    expect(screen.queryByTestId('orb-companion')).not.toBeNull();
    rerender(<VoiceAssistantRuntime visible={false} onModeChange={() => undefined} />);
    expect(screen.queryByTestId('orb-companion')).toBeNull();
    expect(h.stop).not.toHaveBeenCalled(); // no teardown when the workspace opens: same session continues
    expect(h.start).toHaveBeenCalledTimes(1);
  });

  it('backend events drive the orb; alerts pulse it without opening the workspace', async () => {
    render(<VoiceAssistantRuntime visible onModeChange={() => undefined} />);
    await act(async () => { await Promise.resolve(); });
    act(() => {
      h.events?.({ type: 'connection', connected: true });
      h.events?.({ type: 'state', state: 'ASSISTANT_SPEAKING' });
    });
    expect(screen.getByTestId('orb-companion').getAttribute('data-orb-state')).toBe('ASSISTANT_SPEAKING');
    act(() => { h.events?.({ type: 'state', state: 'IDLE' }); });
    act(() => { window.dispatchEvent(new CustomEvent(ALERT_EVENT, { detail: { title: 'High-impact USD event in 10m', summary: '', replay: false, at: 1 } })); });
    expect(screen.getByTestId('orb-companion').getAttribute('data-orb-state')).toBe('ALERT');
    expect(screen.getByTestId('orb-bubble').textContent).toContain('USD event');
    expect(h.expand).not.toHaveBeenCalled();
  });

  it('unmount stops the session once (no duplicate runtime keeps a second socket)', () => {
    const { unmount } = render(<VoiceAssistantRuntime visible onModeChange={() => undefined} />);
    unmount();
    expect(h.stop).toHaveBeenCalledTimes(1);
  });
});
