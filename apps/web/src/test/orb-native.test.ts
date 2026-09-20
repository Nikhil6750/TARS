import { beforeEach, describe, expect, it, vi } from 'vitest';

const calls: Array<[string, unknown]> = [];
let position: [number, number] = [640, 380];

vi.mock('../services/tauri', () => ({ isTauri: () => true }));
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

  it('does nothing when no position was saved, and ignores corrupt data', async () => {
    await orbNative.restorePosition();
    expect(calls).toEqual([]);
    localStorage.setItem('tars.orb.position', '{"nope"');
    await orbNative.restorePosition();
    expect(calls).toEqual([]);
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
});
