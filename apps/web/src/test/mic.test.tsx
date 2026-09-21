import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { deriveOrbState, initialInputs, OrbInputs } from '../orb/orbState';
import { OrbStore } from '../orb/orbStore';
import { MicInfo, judgeMicTest, meterFraction, micStore } from '../orb/micStore';

const calls: Array<[string, unknown]> = [];
vi.mock('../services/tauri', () => ({ isTauri: () => true }));
vi.mock('@tauri-apps/api/core', () => ({
  invoke: async (cmd: string, args?: unknown) => {
    calls.push([cmd, args]);
    if (cmd === 'list_input_devices') return [{ name: 'Realtek Microphone Array', is_default: true }, { name: 'USB Headset', is_default: false }];
    return undefined;
  },
}));

import { MicrophonePanel } from '../components/settings/MicrophonePanel';

const info = (over: Partial<MicInfo> = {}): MicInfo => ({
  status: 'OPEN', device: 'Realtek Microphone Array', selected_by: 'Windows default input', sample_rate: 48000, channels: 2,
  format: 'F32', frames_total: 100, frames_per_sec: 31, rms_db: -60, max_db: -40, silent_for_s: 0, open_for_s: 5, error: null, ...over,
});

const inputs = (over: Partial<OrbInputs> = {}): OrbInputs => ({ ...initialInputs(), connected: true, now: 1, ...over });

describe('orb reflects real microphone health', () => {
  it('a dead or silent microphone is MIC_ERROR, never LISTENING', () => {
    expect(deriveOrbState(inputs({ backend: 'LISTENING', provider: 'GEMINI_LIVE', gemini: 'CONNECTED', mic: 'SILENT' }))).toBe('MIC_ERROR');
    expect(deriveOrbState(inputs({ backend: 'LISTENING', attentionUntil: 99, mic: 'DISCONNECTED' }))).toBe('MIC_ERROR');
    expect(deriveOrbState(inputs({ backend: 'LISTENING', provider: 'GEMINI_LIVE', gemini: 'CONNECTED', mic: 'CONNECTED' }))).toBe('LISTENING');
  });

  it('the startup grace state is not an error; backend disconnect outranks the mic', () => {
    expect(deriveOrbState(inputs({ backend: 'LISTENING', mic: 'STARTING' }))).toBe('IDLE');
    expect(deriveOrbState(inputs({ connected: false, mic: 'SILENT' }))).toBe('DISCONNECTED');
  });

  it('store follows provider_status.microphone and clicking a dead mic says so truthfully', () => {
    const store = new OrbStore(() => 1000);
    store.apply({ type: 'connection', connected: true });
    store.apply({ type: 'provider_status', voice_provider: 'GEMINI_LIVE', providers: { microphone: 'SILENT', gemini_live: 'IDLE' } });
    expect(store.getSnapshot().state).toBe('MIC_ERROR');
    store.attend();
    expect(store.getSnapshot().bubble?.text).toMatch(/silent/i);
    store.apply({ type: 'provider_status', providers: { microphone: 'CONNECTED' } });
    expect(store.getSnapshot().state).not.toBe('MIC_ERROR');
  });
});

describe('microphone self test verdict', () => {
  it('PASS only with meaningful energy; -102 dB is a FAIL', () => {
    expect(judgeMicTest(-30, 150, 'Mic').verdict).toBe('PASS');
    expect(judgeMicTest(-102, 150, 'Mic').verdict).toBe('MICROPHONE SILENT');
    expect(judgeMicTest(-70, 150, 'Mic').verdict).toBe('MICROPHONE SILENT');
    expect(judgeMicTest(-20, 0, 'Mic').verdict).toBe('NO SIGNAL');
  });
  it('meter maps dBFS to a bar', () => {
    expect(meterFraction(-120)).toBe(0);
    expect(meterFraction(-10)).toBe(1);
    expect(meterFraction(-40)).toBeCloseTo(0.5, 5);
  });
});

describe('MicrophonePanel', () => {
  beforeEach(() => { calls.length = 0; localStorage.clear(); sessionStorage.clear(); vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

  it('lists devices, shows the device in use, and persists a chosen microphone natively', async () => {
    act(() => micStore.set(info()));
    render(<MicrophonePanel />);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(screen.getByTestId('mic-device').textContent).toContain('Realtek Microphone Array');
    const select = screen.getByLabelText('Microphone device') as HTMLSelectElement;
    expect([...select.options].map(o => o.text)).toEqual(['Automatic (Windows default)', 'Realtek Microphone Array (default)', 'USB Headset']);
    await act(async () => { fireEvent.change(select, { target: { value: 'USB Headset' } }); await Promise.resolve(); });
    expect(localStorage.getItem('tars.mic.preferred')).toBe('USB Headset');
    expect(calls.some(c => c[0] === 'set_preferred_mic' && (c[1] as { name: string }).name === 'USB Headset')).toBe(true);
  });

  const runTest = async (levels: number[]) => {
    act(() => micStore.set(info({ rms_db: -120, frames_total: 100 })));
    render(<MicrophonePanel />);
    fireEvent.click(screen.getByText('Test microphone'));
    for (const [i, db] of levels.entries()) {
      act(() => micStore.set(info({ rms_db: db, frames_total: 100 + (i + 1) * 20 })));
      await act(async () => { vi.advanceTimersByTime(600); });
    }
    await act(async () => { vi.advanceTimersByTime(5000); });
    return screen.getByTestId('mic-test-result').textContent ?? '';
  };

  it('test PASSES only when real energy arrives', async () => {
    expect(await runTest([-70, -35, -28, -45])).toMatch(/^PASS — Realtek Microphone Array/);
  });

  it('test reports MICROPHONE SILENT with the device name when nothing useful arrives (no Gemini involved)', async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    const text = await runTest([-102, -102, -101, -102]);
    expect(text).toMatch(/^MICROPHONE SILENT — Realtek Microphone Array/);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('warns on persistent digital silence and points at other devices', async () => {
    act(() => micStore.set(info({ rms_db: -120, silent_for_s: 42 })));
    render(<MicrophonePanel />);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(screen.getByTestId('mic-silent-note').textContent).toMatch(/digitally silent for 42 s.*Another input device/);
  });

  it('human test mode shows device, level, VAD, Gemini state, transcript and voice state from real diagnostics', async () => {
    act(() => micStore.set(info({ rms_db: -32 })));
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      json: async () => ({ mic: { frames: 900, db: -32, max_db: -20, vad: true, sent_to_gemini: 640, health: 'CONNECTED' },
        gemini: 'CONNECTED', transcript: 'TARS, can you hear me?', voice_state: 'USER_SPEAKING', voice_provider: 'GEMINI_LIVE' }),
    })));
    render(<MicrophonePanel />);
    fireEvent.click(screen.getByText('Voice diagnostic (human test)'));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    const panel = screen.getByTestId('human-test').textContent ?? '';
    for (const expected of ['Realtek Microphone Array', 'SPEECH', 'CONNECTED', 'TARS, can you hear me?', 'USER_SPEAKING', '900 received / 640 sent']) {
      expect(panel).toContain(expected);
    }
  });
});
