import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { XRayCanvas } from '../marketxray/components/XRayCanvas';
import {
  LABEL_GAP,
  PAD,
  RAIL_MIN_WIDTH,
  placeLabels,
  xrayLayout,
  xrayPeak,
  xrayY
} from '../marketxray/components/xrayLabels';
import type { XRaySeries, XRaySnapshot } from '../marketxray/types';

const SYMBOLS = [
  'AUDCAD', 'AUDCHF', 'AUDJPY', 'AUDUSD', 'CHFJPY', 'EURAUD', 'EURCAD', 'EURGBP', 'EURJPY',
  'EURUSD', 'GBPAUD', 'GBPCAD', 'GBPJPY', 'GBPUSD', 'NZDUSD', 'USDCAD', 'USDJPY', 'XAUUSD'
];

const series = (symbol: string, z: number): XRaySeries => ({
  symbol,
  asset_class: 'FX',
  timeframe: 'H1',
  direction: z >= 0 ? 'BULLISH' : 'BEARISH',
  current_z: z,
  current_close: 1,
  pct_change: 0,
  rank: 1,
  points: [
    { ts: '2026-10-27T09:00:00+00:00', z: 0, close: 1, pct_change: 0 },
    { ts: '2026-10-27T10:00:00+00:00', z, close: 1, pct_change: 0 }
  ]
});

// The reported case: values converged, so every end marker is on nearly one spot.
const converged = SYMBOLS.map((symbol, i) => series(symbol, 0.02 * (i - 9)));
const spread = SYMBOLS.map((symbol, i) => series(symbol, -5.5 + (11 * i) / 17));

const snapshot = (rows: XRaySeries[]): XRaySnapshot => ({
  timeframe: 'H1',
  return_window: 12,
  as_of: '2026-10-27T10:00:00+00:00',
  series: rows,
  skipped: {},
  measures: ''
});

const gaps = (ys: number[]): number[] => ys.slice(1).map((y, i) => y - ys[i]);

describe('X-Ray label placement', () => {
  it('leaves labels where they are when they do not collide', () => {
    const anchors = [{ symbol: 'A', y: 40 }, { symbol: 'B', y: 120 }];
    expect(placeLabels(anchors, 0, 400).labels.map((l) => l.labelY)).toEqual([40, 120]);
  });

  it('separates identical values by the minimum gap, centred on them', () => {
    const anchors = SYMBOLS.map((symbol) => ({ symbol, y: 200 }));
    const { labels, fits } = placeLabels(anchors, 0, 420);
    expect(fits).toBe(true);
    gaps(labels.map((l) => l.labelY)).forEach((gap) => expect(gap).toBeCloseTo(LABEL_GAP));
    const mean = labels.reduce((sum, l) => sum + l.labelY, 0) / labels.length;
    expect(mean).toBeCloseTo(200);
  });

  it('keeps labels in the order of their data, so connectors cannot cross', () => {
    const anchors = SYMBOLS.map((symbol, i) => ({ symbol, y: 200 + ((i * 7) % 11) }));
    const { labels } = placeLabels(anchors, 0, 420);
    const byData = [...anchors].sort((a, b) => a.y - b.y || a.symbol.localeCompare(b.symbol));
    expect(labels.map((l) => l.symbol)).toEqual(byData.map((a) => a.symbol));
  });

  it('never changes the value a label belongs to', () => {
    const anchors = SYMBOLS.map((symbol, i) => ({ symbol, y: 200 + i * 0.1 }));
    const { labels } = placeLabels(anchors, 0, 420);
    labels.forEach((label) => {
      expect(label.y).toBe(anchors.find((a) => a.symbol === label.symbol)?.y);
    });
  });

  it('stays inside the span at both ends', () => {
    for (const y of [-500, 2, 418, 900]) {
      const { labels } = placeLabels(SYMBOLS.map((symbol) => ({ symbol, y })), 7, 413);
      expect(labels[0].labelY).toBeGreaterThanOrEqual(7);
      expect(labels[labels.length - 1].labelY).toBeLessThanOrEqual(413);
      gaps(labels.map((l) => l.labelY)).forEach((gap) => expect(gap).toBeGreaterThan(LABEL_GAP - 0.001));
    }
  });

  it('reports when the span cannot hold them', () => {
    expect(placeLabels(SYMBOLS.map((symbol) => ({ symbol, y: 50 })), 0, 200).fits).toBe(false);
  });

  it('holds any two cases of crowding apart', () => {
    const anchors = [
      ...SYMBOLS.slice(0, 9).map((symbol) => ({ symbol, y: 60 })),
      ...SYMBOLS.slice(9).map((symbol) => ({ symbol, y: 90 }))
    ];
    const ys = placeLabels(anchors, 0, 420).labels.map((l) => l.labelY);
    gaps(ys).forEach((gap) => expect(gap).toBeGreaterThan(LABEL_GAP - 0.001));
  });

  it('anchors each label to the true end of its curve', () => {
    const { labels } = xrayLayout(spread, 900, 420);
    const peak = xrayPeak(spread);
    labels.forEach((label) => {
      const end = spread.find((s) => s.symbol === label.symbol)!;
      expect(label.y).toBeCloseTo(xrayY(end.current_z, 420, peak));
    });
  });

  it.each([
    [1600, 420],
    [1100, 420],
    [800, 320],
    [RAIL_MIN_WIDTH, 320]
  ])('fits all 18 instruments at %ix%i without a collision', (width, height) => {
    for (const rows of [converged, spread]) {
      const layout = xrayLayout(rows, width, height);
      expect(layout.mode).toBe('rail');
      expect(layout.labels.map((l) => l.symbol).sort()).toEqual([...SYMBOLS].sort());
      gaps(layout.labels.map((l) => l.labelY)).forEach((gap) =>
        expect(gap).toBeGreaterThan(LABEL_GAP - 0.001)
      );
      expect(layout.labels[0].labelY).toBeGreaterThanOrEqual(LABEL_GAP / 2);
      expect(layout.labels[17].labelY).toBeLessThanOrEqual(height - LABEL_GAP / 2);
    }
  });

  it('uses the legend when the canvas is too narrow or too short', () => {
    expect(xrayLayout(converged, RAIL_MIN_WIDTH - 1, 420).mode).toBe('legend');
    expect(xrayLayout(converged, 900, PAD.top + 200).mode).toBe('legend');
  });
});

describe('X-Ray instrument labels', () => {
  let width = 900;
  let height = 420;
  let resized: Array<() => void> = [];

  beforeEach(() => {
    width = 900;
    height = 420;
    resized = [];
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(() => width);
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(() => height);
    vi.stubGlobal(
      'ResizeObserver',
      class {
        private readonly callback: () => void;
        constructor(callback: () => void) {
          this.callback = callback;
          resized.push(callback);
        }
        observe() {
          this.callback();
        }
        disconnect() {}
      }
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  const shown = () =>
    [...document.querySelectorAll<HTMLElement>('[data-symbol]')].map((b) => b.dataset.symbol).sort();

  const tops = () =>
    [...document.querySelectorAll<HTMLElement>('.mx-xray-rail [data-symbol]')].map((b) =>
      Number.parseFloat(b.style.top)
    );

  it('offers every instrument as a button, however small its move', () => {
    render(<XRayCanvas snapshot={snapshot(converged)} selected={null} onSelect={() => undefined} />);
    expect(shown()).toEqual([...SYMBOLS].sort());
    expect(screen.getByRole('button', { name: 'EURGBP -0.04 sigma' })).toBeTruthy();
  });

  it('selects the instrument a label is clicked on', () => {
    const onSelect = vi.fn();
    render(<XRayCanvas snapshot={snapshot(converged)} selected="XAUUSD" onSelect={onSelect} />);
    fireEvent.click(screen.getByRole('button', { name: /^USDCAD/ }));
    expect(onSelect).toHaveBeenCalledWith('USDCAD');
    expect(screen.getByRole('button', { name: /^XAUUSD/ }).getAttribute('aria-pressed')).toBe('true');
  });

  it('re-lays the labels when new data arrives', () => {
    const { rerender } = render(
      <XRayCanvas snapshot={snapshot(converged)} selected={null} onSelect={() => undefined} />
    );
    const before = tops();
    rerender(<XRayCanvas snapshot={snapshot(spread)} selected={null} onSelect={() => undefined} />);
    expect(tops()).not.toEqual(before);
    gaps(tops()).forEach((gap) => expect(gap).toBeGreaterThan(LABEL_GAP - 0.001));
  });

  it('moves everything to the legend on a narrow window, and back', () => {
    render(<XRayCanvas snapshot={snapshot(converged)} selected={null} onSelect={() => undefined} />);
    expect(document.querySelector('.mx-xray-rail')).toBeTruthy();

    width = 440;
    act(() => resized.forEach((callback) => callback()));
    expect(document.querySelector('.mx-xray-rail')).toBeNull();
    expect(document.querySelector('.mx-xray-legend')).toBeTruthy();
    expect(shown()).toEqual([...SYMBOLS].sort());

    width = 900;
    act(() => resized.forEach((callback) => callback()));
    expect(document.querySelector('.mx-xray-legend')).toBeNull();
    expect(shown()).toEqual([...SYMBOLS].sort());
  });

  it('shows no labels when there is nothing to label', () => {
    render(<XRayCanvas snapshot={snapshot([])} selected={null} onSelect={() => undefined} loading />);
    expect(shown()).toEqual([]);
    expect(document.querySelector('.mx-xray-legend')).toBeNull();
  });
});
