import { describe, expect, it, vi } from 'vitest';
import { OrbInputs, OrbState, deriveOrbState, initialInputs, levelSource, WAVEFORM_STATES } from '../orb/orbState';
import { OrbStore } from '../orb/orbStore';
import { audioLevels, rmsFromTimeDomain, shapeLevel, smoothLevel } from '../orb/audioLevels';
import { LOOKS, OrbRenderer } from '../orb/orbRenderer';
import { PcmStreamPlayer } from '../services/pcm-player';

const inputs = (over: Partial<OrbInputs> = {}): OrbInputs => ({ ...initialInputs(), connected: true, now: 1000, ...over });

describe('orb state mapping (single authoritative function)', () => {
  it('maps every backend fact to one orb state', () => {
    const cases: Array<[Partial<OrbInputs>, OrbState]> = [
      [{ backend: 'IDLE' }, 'IDLE'],
      [{ backend: 'LISTENING' }, 'IDLE'], // local mic gate alone is calm idle
      [{ backend: 'LISTENING', provider: 'GEMINI_LIVE', gemini: 'CONNECTED' }, 'LISTENING'],
      [{ backend: 'LISTENING', attentionUntil: 5000 }, 'LISTENING'],
      [{ backend: 'USER_SPEAKING' }, 'USER_SPEAKING'],
      [{ backend: 'ENDPOINTING' }, 'USER_SPEAKING'],
      [{ backend: 'INTERRUPTING' }, 'USER_SPEAKING'],
      [{ backend: 'THINKING' }, 'THINKING'],
      [{ backend: 'THINKING', tool: 'desktop_open_app' }, 'TOOL_USE'],
      [{ backend: 'LISTENING', tool: 'get_market_context' }, 'TOOL_USE'],
      [{ backend: 'THINKING', tool: 'ask_claude' }, 'CLAUDE_DEEP_REASONING'],
      [{ backend: 'ASSISTANT_SPEAKING' }, 'ASSISTANT_SPEAKING'],
      [{ backend: 'ERROR' }, 'ERROR'],
      [{ backend: 'LISTENING', provider: 'GEMINI_LIVE', gemini: 'ERROR' }, 'ERROR'],
      [{ connected: false, backend: 'USER_SPEAKING' }, 'DISCONNECTED'],
    ];
    for (const [over, expected] of cases) expect(deriveOrbState(inputs(over)), JSON.stringify(over)).toBe(expected);
  });

  it('alert is a short pulse that never masks speech and then yields back', () => {
    expect(deriveOrbState(inputs({ backend: 'IDLE', alertUntil: 2000 }))).toBe('ALERT');
    expect(deriveOrbState(inputs({ backend: 'USER_SPEAKING', alertUntil: 2000 }))).toBe('USER_SPEAKING');
    expect(deriveOrbState(inputs({ backend: 'ASSISTANT_SPEAKING', alertUntil: 2000 }))).toBe('ASSISTANT_SPEAKING');
    expect(deriveOrbState(inputs({ backend: 'IDLE', alertUntil: 900 }))).toBe('IDLE');
  });

  it('waveform states and level sources', () => {
    expect([...WAVEFORM_STATES].sort()).toEqual(['ASSISTANT_SPEAKING', 'LISTENING', 'USER_SPEAKING']);
    expect(levelSource('USER_SPEAKING')).toBe('mic');
    expect(levelSource('ASSISTANT_SPEAKING')).toBe('assistant');
    expect(levelSource('THINKING')).toBe('none');
  });
});

describe('orb store consumes real backend events', () => {
  const make = () => {
    const t = { now: 10_000 };
    return { t, store: new OrbStore(() => t.now) };
  };

  it('follows the session: connect -> gemini live -> speaking -> tools -> idle', () => {
    const { store } = make();
    expect(store.getSnapshot().state).toBe('DISCONNECTED');
    store.apply({ type: 'connection', connected: true });
    store.apply({ type: 'state', state: 'LISTENING' });
    expect(store.getSnapshot().state).toBe('IDLE');
    store.apply({ type: 'provider_status', voice_provider: 'GEMINI_LIVE', providers: { gemini_live: 'CONNECTED' } });
    expect(store.getSnapshot().state).toBe('LISTENING');
    store.apply({ type: 'state', state: 'USER_SPEAKING' });
    expect(store.getSnapshot().state).toBe('USER_SPEAKING');
    store.apply({ type: 'state', state: 'THINKING' });
    store.apply({ type: 'tool_call', name: 'ask_claude' });
    expect(store.getSnapshot().state).toBe('CLAUDE_DEEP_REASONING');
    store.apply({ type: 'tool_result', name: 'ask_claude', status: 'DONE' });
    expect(store.getSnapshot().state).toBe('THINKING');
    store.apply({ type: 'state', state: 'ASSISTANT_SPEAKING' });
    expect(store.getSnapshot().state).toBe('ASSISTANT_SPEAKING');
    store.apply({ type: 'connection', connected: false });
    expect(store.getSnapshot().state).toBe('DISCONNECTED');
  });

  it('desktop actions: tool use, success flash, failure bubble, confirmation card', () => {
    const { t, store } = make();
    store.apply({ type: 'connection', connected: true });
    store.apply({ type: 'tool_call', name: 'desktop_open_app' });
    expect(store.getSnapshot().state).toBe('TOOL_USE');
    store.apply({ type: 'tool_result', name: 'desktop_open_app', status: 'DONE' });
    expect(store.getSnapshot().flashUntil).toBeGreaterThan(t.now);
    store.apply({ type: 'tool_call', name: 'desktop_focus_window' });
    store.apply({ type: 'tool_result', name: 'desktop_focus_window', status: 'NOT_FOUND' });
    expect(store.getSnapshot().bubble?.kind).toBe('error');
    store.apply({ type: 'confirmation_pending', text: 'Click Save?' });
    expect(store.getSnapshot().confirm?.text).toBe('Click Save?');
    store.apply({ type: 'confirmation_cleared' });
    expect(store.getSnapshot().confirm).toBeNull();
  });

  it('transcript is temporary and short-lived', () => {
    vi.useFakeTimers();
    const { t, store } = make();
    store.apply({ type: 'connection', connected: true });
    store.apply({ type: 'partial_transcript', text: 'check gold' });
    expect(store.getSnapshot().transcript).toBe('check gold');
    t.now += 3000;
    vi.advanceTimersByTime(3200);
    expect(store.getSnapshot().transcript).toBe('');
    vi.useRealTimers();
  });

  it('long assistant text is truncated to a short caption', () => {
    const { store } = make();
    store.apply({ type: 'delta', text: 'word '.repeat(80) });
    const caption = store.getSnapshot().caption;
    expect(caption.length).toBeLessThanOrEqual(112);
    expect(caption.startsWith('…')).toBe(true);
  });

  it('alert pulses briefly, returns to the prior state, and stays available as "recent alert"', () => {
    vi.useFakeTimers();
    const { t, store } = make();
    store.apply({ type: 'connection', connected: true });
    store.alert('High-impact USD event in 10m');
    expect(store.getSnapshot().state).toBe('ALERT');
    expect(store.getSnapshot().bubble?.text).toContain('USD');
    t.now += 2500;
    vi.advanceTimersByTime(2600);
    expect(store.getSnapshot().state).toBe('IDLE');
    t.now += 8000;
    vi.advanceTimersByTime(8100);
    expect(store.getSnapshot().bubble).toBeNull();
    expect(store.getSnapshot().lastAlert?.text).toContain('USD');
    store.showLastAlert();
    expect(store.getSnapshot().bubble?.text).toContain('USD');
    vi.useRealTimers();
  });

  it('truthful local-fallback notice, and interrupt clears tool/caption', () => {
    const { store } = make();
    store.apply({ type: 'connection', connected: true });
    store.apply({ type: 'provider_status', voice_provider: 'LOCAL_STREAMING', detail: 'Using LOCAL_STREAMING voice: GEMINI_API_KEY is not set' });
    expect(store.getSnapshot().bubble?.text).toBe('Using local voice');
    store.apply({ type: 'tool_call', name: 'ask_claude' });
    store.apply({ type: 'interrupt' });
    expect(store.getSnapshot().state).toBe('IDLE');
  });

  it('subscribers are notified only when something visible changes', () => {
    const { store } = make();
    const fn = vi.fn();
    store.subscribe(fn);
    store.apply({ type: 'connection', connected: true });
    const n = fn.mock.calls.length;
    store.apply({ type: 'connection', connected: true });
    store.apply({ type: 'unknown_event' });
    expect(fn.mock.calls.length).toBe(n);
  });
});

describe('real audio energy drives the orb (fast attack, slow decay)', () => {
  it('attack is much faster than decay', () => {
    const up = smoothLevel(0, 1, 33);
    const down = 1 - smoothLevel(1, 0, 33);
    expect(up).toBeGreaterThan(0.5);
    expect(down).toBeLessThan(0.2);
    expect(up).toBeGreaterThan(down * 3);
  });

  it('shapes quiet speech visible but caps loud speech', () => {
    expect(shapeLevel(0.05)).toBeGreaterThan(0.15);
    expect(shapeLevel(5)).toBe(1);
    expect(shapeLevel(-1)).toBe(0);
  });

  it('microphone level comes from pushed frames and decays to zero when frames stop', () => {
    audioLevels.pushMic(0.7, 1000);
    expect(audioLevels.mic(1100)).toBe(0.7);
    expect(audioLevels.mic(1400)).toBe(0);
  });

  it('assistant level comes from the registered analyser reader, with an honest local fallback', () => {
    audioLevels.setAssistantReader(() => 0.55);
    expect(audioLevels.assistant()).toBe(0.55);
    audioLevels.setAssistantReader(() => 0);
    audioLevels.setAssistantFallback(0.4);
    expect(audioLevels.assistant()).toBe(0.4);
    audioLevels.setAssistantReader(null);
    audioLevels.setAssistantFallback(0);
    expect(audioLevels.assistant()).toBe(0);
  });

  it('rms of a time-domain buffer: silence = 0, signal > 0', () => {
    expect(rmsFromTimeDomain(new Uint8Array(256).fill(128))).toBe(0);
    const loud = new Uint8Array(256).map((_, i) => (i % 2 ? 200 : 56));
    expect(rmsFromTimeDomain(loud)).toBeGreaterThan(0.5);
  });

  it('PcmStreamPlayer.level() reads the AnalyserNode of the actually playing audio', () => {
    const data = new Uint8Array(512).map((_, i) => (i % 2 ? 190 : 66));
    const started: Array<() => void> = [];
    class FakeCtx {
      state = 'running';
      currentTime = 0;
      destination = {};
      createAnalyser() {
        return { fftSize: 512, connect: vi.fn(), getByteTimeDomainData: (b: Uint8Array) => b.set(data) };
      }
      createBuffer(_c: number, n: number, rate: number) { return { duration: n / rate, getChannelData: () => new Float32Array(n) }; }
      createBufferSource() {
        const src = { buffer: null, connect: vi.fn(), start: vi.fn(), stop: vi.fn(), onended: null as null | (() => void) };
        started.push(() => src.onended?.());
        return src;
      }
      resume() { return Promise.resolve(); }
    }
    vi.stubGlobal('AudioContext', FakeCtx);
    const player = new PcmStreamPlayer();
    expect(player.level()).toBe(0); // nothing playing -> silent even if the analyser has data
    player.play(btoa(String.fromCharCode(...new Array(200).fill(1))));
    expect(player.level()).toBeGreaterThan(0.3);
    player.stop();
    expect(player.level()).toBe(0);
    vi.unstubAllGlobals();
  });
});

function fakeCtx() {
  const gradients: number[][] = [];
  const calls: Record<string, number> = {};
  const target: Record<string, unknown> = {
    createRadialGradient: (...a: number[]) => { gradients.push(a); return { addColorStop: () => undefined }; },
  };
  const ctx = new Proxy(target, {
    get(t, k: string) {
      if (k in t) return t[k];
      return (..._a: unknown[]) => { calls[k] = (calls[k] ?? 0) + 1; };
    },
    set(t, k: string, v) { t[k] = v; return true; },
  }) as unknown as CanvasRenderingContext2D;
  return { ctx, gradients, calls };
}

describe('orb renderer', () => {
  const states = Object.keys(LOOKS) as OrbState[];

  it('draws every state without error and eases toward the state look', () => {
    const { ctx } = fakeCtx();
    const r = new OrbRenderer(ctx, 168);
    for (const s of states) {
      for (let i = 0; i < 90; i++) r.draw({ t: i / 60, dt: 1 / 60, state: s, level: 0.3, flash: 0, reduced: false });
      expect(Math.abs(r.look.scale - LOOKS[s].scale)).toBeLessThan(0.02);
      expect(Math.abs(r.look.dim - LOOKS[s].dim)).toBeLessThan(0.02);
    }
  });

  it('a state change morphs smoothly instead of jumping', () => {
    const { ctx } = fakeCtx();
    const r = new OrbRenderer(ctx, 168);
    r.snapTo('IDLE');
    r.draw({ t: 0, dt: 1 / 60, state: 'ALERT', level: 0, flash: 0, reduced: false });
    const one = r.look.glow;
    expect(one).toBeGreaterThan(LOOKS.IDLE.glow);
    expect(one).toBeLessThan(LOOKS.ALERT.glow * 0.95);
  });

  it('audio energy visibly deforms/expands the orb (louder = larger glow radius), only in voice states', () => {
    const outerGlowRadius = (state: OrbState, level: number) => {
      const { ctx, gradients } = fakeCtx();
      const r = new OrbRenderer(ctx, 1000);
      r.snapTo(state);
      r.draw({ t: 1, dt: 0.016, state, level, flash: 0, reduced: false });
      return gradients[0][5];
    };
    expect(outerGlowRadius('USER_SPEAKING', 0.25)).toBeGreaterThan(outerGlowRadius('USER_SPEAKING', 0) + 15);
    expect(outerGlowRadius('ASSISTANT_SPEAKING', 0.25)).toBeGreaterThan(outerGlowRadius('ASSISTANT_SPEAKING', 0) + 15);
    // states that do not express audio ignore level
    expect(outerGlowRadius('THINKING', 0.9)).toBeCloseTo(outerGlowRadius('THINKING', 0), 5);
  });

  it('user speaking and assistant speaking look different', () => {
    expect(LOOKS.USER_SPEAKING.a).not.toEqual(LOOKS.ASSISTANT_SPEAKING.a);
    expect(LOOKS.CLAUDE_DEEP_REASONING.a).not.toEqual(LOOKS.THINKING.a);
  });

  it('reduced motion renders a static frame (no rotation, no audio deformation)', () => {
    const a = fakeCtx();
    const b = fakeCtx();
    const ra = new OrbRenderer(a.ctx, 168);
    const rb = new OrbRenderer(b.ctx, 168);
    ra.snapTo('USER_SPEAKING'); rb.snapTo('USER_SPEAKING');
    ra.draw({ t: 1, dt: 0.016, state: 'USER_SPEAKING', level: 0.9, flash: 0, reduced: true });
    rb.draw({ t: 99, dt: 0.016, state: 'USER_SPEAKING', level: 0, flash: 0, reduced: true });
    expect(a.gradients).toEqual(b.gradients);
  });
});
