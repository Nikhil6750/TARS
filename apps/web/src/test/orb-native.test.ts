import { beforeEach, describe, expect, it, vi } from 'vitest';

const calls: Array<[string, unknown]> = [];
let position: [number, number] = [640, 380];

vi.mock('../services/tauri', () => ({ isTauri: () => true }));
let moveHandler: (() => void) | null = null;
vi.mock('@tauri-apps/api/event', () => ({ listen: async (_e: string, cb: () => void) => { moveHandler = cb; return () => { moveHandler = null; }; } }));
vi.mock('@tauri-apps/api/core', () => ({
  invoke: async (cmd: string, args?: unknown) => {
    calls.push([cmd, args]);
    if (cmd === 'orb_get_position') return position;
    return undefined;
  },
}));

import { orbNative } from '../orb/orbNative';

describe('orb position persistence (drag)', () => {
  beforeEach(() => { calls.length = 0; localStorage.clear(); });

  it('saves the position after a drag and restores + re-summons the orb on next launch', async () => {
    await orbNative.savePosition();
    expect(JSON.parse(localStorage.getItem('tars.orb.position') ?? 'null')).toEqual([640, 380]);
    calls.length = 0;
    await orbNative.restorePosition();
    expect(calls[0]).toEqual(['orb_set_saved_position', { x: 640, y: 380 }]);
    expect(calls[1][0]).toBe('summon_hud');
    expect(calls[1][1]).toEqual({ mode: 'voice' });
  });

  it('without a saved position it still applies the orb layout once, and ignores corrupt data', async () => {
    await orbNative.restorePosition();
    expect(calls.map(c => c[0])).toEqual(['summon_hud']);
    calls.length = 0;
    localStorage.setItem('tars.orb.position', '{"nope"');
    await orbNative.restorePosition();
    expect(calls.map(c => c[0])).toEqual(['summon_hud']);
  });

  it('drag and hit-region commands go through the existing Tauri window commands', async () => {
    await orbNative.startDrag();
    await orbNative.setHitRegions([[70, 24, 120, 120]]);
    expect(calls).toEqual([['orb_start_drag', undefined], ['orb_set_hit_regions', { regions: [[70, 24, 120, 120]] }]]);
  });

  it('expand and collapse are the native summon modes', async () => {
    await orbNative.expand();
    await orbNative.collapse();
    expect(calls.map(c => [c[0], (c[1] as { mode: string }).mode])).toEqual([['summon_hud', 'workstation'], ['summon_hud', 'voice']]);
  });

  it('saves the position when the window actually moves (the OS drag loop swallows pointer-up), only in orb mode', async () => {
    vi.useFakeTimers();
    const off = await orbNative.watchMoves();
    document.documentElement.dataset.mode = 'workspace';
    moveHandler?.();
    await vi.advanceTimersByTimeAsync(700);
    expect(calls).toEqual([]);
    document.documentElement.dataset.mode = 'orb';
    moveHandler?.();
    moveHandler?.();
    await vi.advanceTimersByTimeAsync(700);
    expect(calls.filter(c => c[0] === 'orb_get_position').length).toBe(1); // debounced
    expect(localStorage.getItem('tars.orb.position')).toBe('[640,380]');
    off();
    vi.useRealTimers();
  });
});
