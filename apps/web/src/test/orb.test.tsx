import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { OrbCompanion, OrbActions } from '../components/orb/OrbCompanion';
import { TarsOrb } from '../components/orb/TarsOrb';
import { OrbStore } from '../orb/orbStore';
import { OrbState } from '../orb/orbState';

function installCanvas() {
  const ctx = new Proxy({ createRadialGradient: () => ({ addColorStop: () => undefined }) } as Record<string, unknown>, {
    get: (t, k: string) => (k in t ? t[k] : () => undefined),
    set: (t, k: string, v) => { t[k] = v; return true; },
  });
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation((() => ctx) as never);
}

const actions = (): OrbActions => ({
  wake: vi.fn(), setMuted: vi.fn(), confirm: vi.fn(), openWorkspace: vi.fn(), quit: vi.fn(),
});

function liveStore(state: string = 'LISTENING', gemini = 'CONNECTED') {
  const clock = { now: 10_000 };
  const store = new OrbStore(() => clock.now);
  store.apply({ type: 'connection', connected: true });
  store.apply({ type: 'provider_status', voice_provider: 'GEMINI_LIVE', providers: { gemini_live: gemini } });
  store.apply({ type: 'state', state });
  return { store, clock };
}

beforeEach(() => {
  // jsdom has no PointerEvent: MouseEvent carries button/clientX which is all the orb reads.
  if (!('PointerEvent' in window)) (window as unknown as { PointerEvent: unknown }).PointerEvent = MouseEvent;
  installCanvas();
  vi.stubGlobal('matchMedia', (q: string) => ({ matches: false, media: q, addEventListener: () => undefined, removeEventListener: () => undefined }));
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('orb replaces the compact HUD', () => {
  it('renders only an orb in compact mode: no rectangular HUD, no permanent status/market text', () => {
    const { store } = liveStore('IDLE');
    const { container } = render(<OrbCompanion store={store} actions={actions()} />);
    expect(screen.getByRole('button', { name: /TARS/ })).toBeTruthy();
    expect(screen.getByTestId('tars-orb-canvas')).toBeTruthy();
    const text = container.textContent ?? '';
    for (const forbidden of ['MT5', 'TV', 'CAL', 'NEWS', 'EURUSD', 'Say "Hey TARS"', 'No live quote', 'Listening', 'LISTENING']) {
      expect(text).not.toContain(forbidden);
    }
    expect(screen.queryByTestId('monitor-strip')).toBeNull();
    expect((container.firstChild as HTMLElement).style.background).toBe('transparent');
  });

  it('old HUD components are gone from the source tree', () => {
    const base = path.resolve(__dirname, '..');
    expect(fs.existsSync(path.join(base, 'components/voice/VoicePanel.tsx'))).toBe(false);
    expect(fs.existsSync(path.join(base, 'components/voice/FloatingVoicePanel.tsx'))).toBe(false);
  });

  it.each<[string, OrbState]>([
    ['IDLE', 'IDLE'], ['LISTENING', 'LISTENING'], ['USER_SPEAKING', 'USER_SPEAKING'], ['THINKING', 'THINKING'],
    ['ASSISTANT_SPEAKING', 'ASSISTANT_SPEAKING'],
  ])('backend state %s shows orb state %s', (backend, expected) => {
    const { store } = liveStore(backend);
    render(<OrbCompanion store={store} actions={actions()} />);
    expect(screen.getByTestId('orb-companion').getAttribute('data-orb-state')).toBe(expected);
    expect(screen.getByTestId('tars-orb-canvas').getAttribute('data-orb-state')).toBe(expected);
  });

  it('alert and error states', () => {
    const { store } = liveStore('IDLE', 'IDLE');
    render(<OrbCompanion store={store} actions={actions()} />);
    act(() => store.alert('High-impact USD event in 10m'));
    expect(screen.getByTestId('orb-companion').getAttribute('data-orb-state')).toBe('ALERT');
    expect(screen.getByTestId('orb-bubble').textContent).toContain('USD event');
    act(() => store.apply({ type: 'state', state: 'ERROR' }));
    expect(screen.getByTestId('orb-companion').getAttribute('data-orb-state')).toBe('ERROR');
  });

  it('clicking the alert bubble opens the workspace', () => {
    const { store } = liveStore('IDLE', 'IDLE');
    const a = actions();
    render(<OrbCompanion store={store} actions={a} />);
    act(() => store.alert('EURUSD volatility increased'));
    fireEvent.click(screen.getByTestId('orb-bubble'));
    expect(a.openWorkspace).toHaveBeenCalled();
  });
});

describe('waveform and transcript are situational', () => {
  it('waveform shows only during voice activity and disappears after', () => {
    vi.useFakeTimers();
    const { store } = liveStore('IDLE', 'IDLE');
    render(<OrbCompanion store={store} actions={actions()} />);
    expect(screen.queryByTestId('orb-waveform')).toBeNull();
    for (const state of ['LISTENING', 'USER_SPEAKING', 'ASSISTANT_SPEAKING']) {
      act(() => { store.apply({ type: 'provider_status', voice_provider: 'GEMINI_LIVE', providers: { gemini_live: 'CONNECTED' } }); store.apply({ type: 'state', state }); });
      expect(screen.queryByTestId('orb-waveform'), state).not.toBeNull();
    }
    act(() => store.apply({ type: 'state', state: 'THINKING' }));
    act(() => { vi.advanceTimersByTime(500); });
    expect(screen.queryByTestId('orb-waveform')).toBeNull();
  });

  it('shows a short live transcript then fades it away; long text never becomes a card', () => {
    vi.useFakeTimers();
    const { store, clock } = liveStore('USER_SPEAKING');
    render(<OrbCompanion store={store} actions={actions()} />);
    act(() => store.apply({ type: 'partial_transcript', text: 'check gold' }));
    expect(screen.getByTestId('orb-transcript').textContent).toContain('check gold');
    clock.now += 3000;
    act(() => { vi.advanceTimersByTime(3200); });
    expect(screen.queryByTestId('orb-transcript')).toBeNull();
  });
});

describe('interactions', () => {
  it('single click wakes/attends after the double-click window; double click expands the workspace', () => {
    vi.useFakeTimers();
    const { store } = liveStore('LISTENING', 'IDLE');
    const a = actions();
    render(<OrbCompanion store={store} actions={a} />);
    const hit = screen.getByTestId('orb-hit');
    fireEvent.pointerDown(hit, { button: 0, clientX: 10, clientY: 10 });
    fireEvent.pointerUp(hit, { clientX: 10, clientY: 10 });
    expect(a.wake).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(300); });
    expect(a.wake).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('orb-companion').getAttribute('data-orb-state')).toBe('LISTENING'); // attention

    fireEvent.pointerDown(hit, { button: 0, clientX: 10, clientY: 10 });
    fireEvent.pointerUp(hit, { clientX: 10, clientY: 10 });
    fireEvent.pointerDown(hit, { button: 0, clientX: 10, clientY: 10 });
    fireEvent.pointerUp(hit, { clientX: 10, clientY: 10 });
    expect(a.openWorkspace).toHaveBeenCalledTimes(1);
    act(() => { vi.advanceTimersByTime(400); });
    expect(a.wake).toHaveBeenCalledTimes(1);
  });

  it('dragging never counts as a click (no accidental voice activation)', () => {
    vi.useFakeTimers();
    const { store } = liveStore('IDLE', 'IDLE');
    const a = actions();
    render(<OrbCompanion store={store} actions={a} />);
    const hit = screen.getByTestId('orb-hit');
    fireEvent.pointerDown(hit, { button: 0, clientX: 10, clientY: 10 });
    fireEvent.pointerMove(hit, { clientX: 60, clientY: 40 });
    fireEvent.pointerUp(hit, { clientX: 60, clientY: 40 });
    act(() => { vi.advanceTimersByTime(600); });
    expect(a.wake).not.toHaveBeenCalled();
    expect(a.openWorkspace).not.toHaveBeenCalled();
  });

  it('keyboard: Enter activates, Shift+Enter opens the workspace, ContextMenu opens the menu', () => {
    vi.useFakeTimers();
    const { store } = liveStore('IDLE', 'IDLE');
    const a = actions();
    render(<OrbCompanion store={store} actions={a} />);
    const hit = screen.getByTestId('orb-hit');
    fireEvent.keyDown(hit, { key: 'Enter' });
    expect(a.wake).toHaveBeenCalled();
    fireEvent.keyDown(hit, { key: 'Enter', shiftKey: true });
    expect(a.openWorkspace).toHaveBeenCalled();
    fireEvent.keyDown(hit, { key: 'ContextMenu' });
    expect(screen.getByRole('menu', { name: 'TARS options' })).toBeTruthy();
  });

  it('right-click menu offers only supported items and they work', () => {
    const { store } = liveStore('IDLE', 'IDLE');
    const a = actions();
    render(<OrbCompanion store={store} actions={a} />);
    fireEvent.contextMenu(screen.getByTestId('orb-hit'));
    const labels = screen.getAllByRole('menuitem').map(el => el.textContent);
    expect(labels).toEqual(['Open TARS', 'Start listening', 'Mute microphone', 'Settings', 'Quit TARS']);
    fireEvent.click(screen.getByRole('menuitem', { name: 'Mute microphone' }));
    expect(a.setMuted).toHaveBeenCalledWith(true);
    fireEvent.contextMenu(screen.getByTestId('orb-hit'));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Settings' }));
    expect(a.openWorkspace).toHaveBeenCalledWith('settings');
    fireEvent.contextMenu(screen.getByTestId('orb-hit'));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Quit TARS' }));
    expect(a.quit).toHaveBeenCalled();
  });

  it('"Recent alert" appears only after an alert exists', () => {
    const { store } = liveStore('IDLE', 'IDLE');
    render(<OrbCompanion store={store} actions={actions()} />);
    act(() => store.alert('Fed speaker at 3pm'));
    fireEvent.contextMenu(screen.getByTestId('orb-hit'));
    expect(screen.getByRole('menuitem', { name: 'Recent alert' })).toBeTruthy();
  });

  it('confirmation card: Yes/No go to the backend (ActionRuntime stays authoritative)', () => {
    const { store } = liveStore('THINKING');
    const a = actions();
    render(<OrbCompanion store={store} actions={a} />);
    act(() => store.apply({ type: 'confirmation_pending', text: 'Click Save in Notepad?' }));
    expect(screen.getByRole('alertdialog').textContent).toContain('Click Save in Notepad?');
    fireEvent.click(screen.getByRole('button', { name: 'Yes' }));
    expect(a.confirm).toHaveBeenCalledWith(true);
    fireEvent.click(screen.getByRole('button', { name: 'No' }));
    expect(a.confirm).toHaveBeenCalledWith(false);
  });
});

describe('reduced motion and cost', () => {
  it('honours prefers-reduced-motion: no animation loop', () => {
    vi.stubGlobal('matchMedia', (q: string) => ({ matches: true, media: q, addEventListener: () => undefined, removeEventListener: () => undefined }));
    const raf = vi.spyOn(window, 'requestAnimationFrame');
    render(<TarsOrb state="LISTENING" flashUntil={0} />);
    expect(raf).not.toHaveBeenCalled();
  });

  it('runs an animation loop when motion is allowed and stops on unmount', () => {
    const raf = vi.spyOn(window, 'requestAnimationFrame');
    const cancel = vi.spyOn(window, 'cancelAnimationFrame');
    const { unmount } = render(<TarsOrb state="USER_SPEAKING" flashUntil={0} />);
    expect(raf).toHaveBeenCalled();
    unmount();
    expect(cancel).toHaveBeenCalled();
  });
});

describe('single runtime / single session', () => {
  it('App mounts exactly one VoiceAssistantRuntime and it is the only orb entry point', () => {
    const src = fs.readFileSync(path.resolve(__dirname, '../App.tsx'), 'utf-8');
    expect(src.match(/<VoiceAssistantRuntime/g)?.length).toBe(1);
    expect(src).not.toMatch(/VoicePanel|FloatingVoicePanel/);
  });

  it('the realtime client is a singleton and ignores a second start()', async () => {
    const { realtimeVoiceClient, RealtimeVoiceClient } = await import('../runtime/RealtimeVoiceClient');
    expect(realtimeVoiceClient).toBeInstanceOf(RealtimeVoiceClient);
    const source = fs.readFileSync(path.resolve(__dirname, '../runtime/RealtimeVoiceClient.ts'), 'utf-8');
    expect(source).toMatch(/if \(!isTauri\(\) \|\| !this\.stopped\) return;/);
    // orb components never open their own socket
    for (const f of ['components/orb/OrbCompanion.tsx', 'components/orb/TarsOrb.tsx', 'orb/orbStore.ts']) {
      expect(fs.readFileSync(path.resolve(__dirname, '..', f), 'utf-8')).not.toMatch(/new WebSocket/);
    }
  });
});
