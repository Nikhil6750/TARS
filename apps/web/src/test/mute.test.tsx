import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { OrbCompanion, OrbActions } from '../components/orb/OrbCompanion';
import { OrbStore } from '../orb/orbStore';
import { muteStore } from '../orb/muteStore';

let nativeMuted = false;
const invokeCalls: Array<[string, unknown]> = [];
let muteListener: ((e: { payload: boolean }) => void) | null = null;

vi.mock('../services/tauri', () => ({ isTauri: () => true }));
vi.mock('@tauri-apps/api/core', () => ({
  invoke: async (cmd: string, args?: { muted?: boolean }) => {
    invokeCalls.push([cmd, args]);
    if (cmd === 'get_mic_muted') return nativeMuted;
    if (cmd === 'set_mic_muted') { nativeMuted = !!args?.muted; muteListener?.({ payload: nativeMuted }); } // native authority echoes
    return undefined;
  },
}));
vi.mock('@tauri-apps/api/event', () => ({
  listen: async (_e: string, cb: (e: { payload: boolean }) => void) => { muteListener = cb; return () => { muteListener = null; }; },
}));
vi.mock('../orb/orbNative', () => ({
  orbNative: { setHitRegions: vi.fn(), startDrag: vi.fn(), savePosition: vi.fn(), expand: vi.fn(), collapse: vi.fn(), quit: vi.fn() },
}));

import { MicrophonePanel } from '../components/settings/MicrophonePanel';
import { RealtimeVoiceClient } from '../runtime/RealtimeVoiceClient';

function harness() {
  const store = new OrbStore(() => 1000);
  store.apply({ type: 'connection', connected: true });
  store.apply({ type: 'provider_status', voice_provider: 'GEMINI_LIVE', providers: { microphone: 'CONNECTED', gemini_live: 'IDLE' } });
  store.apply({ type: 'state', state: 'LISTENING' });
  // the same wiring VoiceAssistantRuntime does: native mute -> orb store
  const off = muteStore.subscribe(() => store.setMuted(muteStore.getSnapshot()));
  const actions: OrbActions = {
    wake: vi.fn(), setMuted: (m: boolean) => void muteStore.set(m), confirm: vi.fn(), openWorkspace: vi.fn(), quit: vi.fn(),
  };
  const wake = actions.wake as ReturnType<typeof vi.fn>;
  const open = actions.openWorkspace as ReturnType<typeof vi.fn>;
  return { store, actions, wake, open, off };
}

beforeEach(() => {
  nativeMuted = false;
  invokeCalls.length = 0;
  muteListener = null;
  muteStore.reset();
  if (!('PointerEvent' in window)) (window as unknown as { PointerEvent: unknown }).PointerEvent = MouseEvent;
  const ctx = new Proxy({ createRadialGradient: () => ({ addColorStop: () => undefined }) } as Record<string, unknown>, {
    get: (t, k: string) => (k in t ? t[k] : () => undefined), set: (t, k: string, v) => { t[k] = v; return true; },
  });
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation((() => ctx) as never);
  vi.stubGlobal('matchMedia', (q: string) => ({ matches: false, media: q, addEventListener: () => undefined, removeEventListener: () => undefined }));
  vi.useFakeTimers();
});
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('mute button beside the orb', () => {
  it('shows the unmuted state with an accessible label and tooltip', () => {
    const h = harness();
    render(<OrbCompanion store={h.store} actions={h.actions} />);
    const b = screen.getByTestId('orb-mute');
    expect(b.getAttribute('aria-label')).toBe('Mute microphone');
    expect(b.getAttribute('title')).toBe('Microphone active');
    expect(b.getAttribute('aria-pressed')).toBe('false');
    expect(screen.getByTestId('icon-mic')).toBeTruthy();
    expect(screen.getByTestId('orb-companion').getAttribute('data-orb-state')).toBe('IDLE'); // not an error look
    h.off();
  });

  it('click mutes through the native authority, click again unmutes; icon and label follow', async () => {
    const h = harness();
    await act(async () => { await muteStore.start(); });
    render(<OrbCompanion store={h.store} actions={h.actions} />);
    await act(async () => { fireEvent.click(screen.getByTestId('orb-mute')); await Promise.resolve(); await Promise.resolve(); });
    expect(invokeCalls.filter(c => c[0] === 'set_mic_muted')).toEqual([['set_mic_muted', { muted: true }]]);
    expect(nativeMuted).toBe(true);
    let b = screen.getByTestId('orb-mute');
    expect(b.getAttribute('aria-label')).toBe('Unmute microphone');
    expect(b.getAttribute('title')).toBe('Microphone muted');
    expect(b.getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByTestId('icon-mic-off')).toBeTruthy();
    const state = screen.getByTestId('orb-companion').getAttribute('data-orb-state');
    expect(state).not.toBe('ERROR');
    expect(state).not.toBe('MIC_ERROR');
    await act(async () => { fireEvent.click(screen.getByTestId('orb-mute')); await Promise.resolve(); await Promise.resolve(); });
    b = screen.getByTestId('orb-mute');
    expect(nativeMuted).toBe(false);
    expect(b.getAttribute('aria-label')).toBe('Mute microphone');
    expect(screen.getByTestId('icon-mic')).toBeTruthy();
    h.off();
  });

  it('is isolated from the orb: no wake, no workspace, no drag, no menu from any mute-button gesture', async () => {
    const h = harness();
    render(<OrbCompanion store={h.store} actions={h.actions} />);
    const b = screen.getByTestId('orb-mute');
    fireEvent.pointerDown(b, { button: 0, clientX: 5, clientY: 5 });
    fireEvent.pointerMove(b, { clientX: 80, clientY: 60 });
    fireEvent.pointerUp(b, { clientX: 80, clientY: 60 });
    fireEvent.click(b);
    fireEvent.doubleClick(b);
    fireEvent.contextMenu(b);
    fireEvent.keyDown(b, { key: 'Enter' });
    await act(async () => { vi.advanceTimersByTime(800); });
    expect(h.wake).not.toHaveBeenCalled();
    expect(h.open).not.toHaveBeenCalled();
    expect(screen.queryByRole('menu')).toBeNull();
    h.off();
  });

  it('the orb itself still behaves: single click wakes, double click opens the workspace', async () => {
    const h = harness();
    render(<OrbCompanion store={h.store} actions={h.actions} />);
    const hit = screen.getByTestId('orb-hit');
    fireEvent.pointerDown(hit, { button: 0, clientX: 10, clientY: 10 });
    fireEvent.pointerUp(hit, { clientX: 10, clientY: 10 });
    await act(async () => { vi.advanceTimersByTime(300); });
    expect(h.wake).toHaveBeenCalledTimes(1);
    for (let i = 0; i < 2; i++) {
      fireEvent.pointerDown(hit, { button: 0, clientX: 10, clientY: 10 });
      fireEvent.pointerUp(hit, { clientX: 10, clientY: 10 });
    }
    expect(h.open).toHaveBeenCalledTimes(1);
    h.off();
  });

  it('the mute button is keyboard reachable', () => {
    const h = harness();
    render(<OrbCompanion store={h.store} actions={h.actions} />);
    const b = screen.getByTestId('orb-mute') as HTMLButtonElement;
    b.focus();
    expect(document.activeElement).toBe(b);
    expect(b.tabIndex).toBeGreaterThanOrEqual(0);
    h.off();
  });
});

describe('state ownership and persistence', () => {
  it('a restart reads the persisted native value: muted stays muted, unmuted stays unmuted', async () => {
    nativeMuted = true;
    await muteStore.start();
    expect(muteStore.getSnapshot()).toBe(true);
    muteStore.reset();
    muteStore.apply(false);
    nativeMuted = false;
    await muteStore.start();
    expect(muteStore.getSnapshot()).toBe(false);
  });

  it('tray/native changes reach every view; orb button and Settings switch always agree', async () => {
    const h = harness();
    await act(async () => { await muteStore.start(); });
    render(<><OrbCompanion store={h.store} actions={h.actions} /><MicrophonePanel /></>);
    const sw = screen.getByTestId('settings-mute') as HTMLInputElement;
    expect(sw.checked).toBe(false);
    // tray "Mute Microphone": native flips the value and emits the event
    await act(async () => { nativeMuted = true; muteListener?.({ payload: true }); });
    expect(sw.checked).toBe(true);
    expect(screen.getByTestId('orb-mute').getAttribute('aria-label')).toBe('Unmute microphone');
    // Settings switch: goes through the same authority
    await act(async () => { fireEvent.click(sw); await Promise.resolve(); await Promise.resolve(); });
    expect(nativeMuted).toBe(false);
    expect(sw.checked).toBe(false);
    expect(screen.getByTestId('orb-mute').getAttribute('aria-label')).toBe('Mute microphone');
    h.off();
  });

  it('the backend is told: the realtime client sends the mute state', () => {
    const client = new RealtimeVoiceClient();
    const sent: string[] = [];
    (client as unknown as { socket: unknown }).socket = { readyState: WebSocket.OPEN, send: (m: string) => sent.push(m) };
    client.setMuted(true);
    client.setMuted(false);
    expect(sent.map(m => JSON.parse(m))).toEqual([{ type: 'mute', muted: true }, { type: 'mute', muted: false }]);
  });
});
