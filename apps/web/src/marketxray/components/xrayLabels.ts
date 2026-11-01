/**
 * Where the X-Ray puts its instrument labels.
 *
 * A label sits as near its curve's end as it can without touching a neighbour.
 * Only the label moves: the curve's end marker stays at its real value, and a
 * connector ties the two together. When the rail cannot hold every label, all of
 * them move to a scrollable legend below the plot, so an instrument is never
 * dropped to make room.
 */
import type { XRaySeries } from '../types';

export const PAD = { top: 20, bottom: 24, left: 46 };
// Connector run plus the label itself: `GBPCAD -6.00σ` at 9.5px.
export const RAIL_CONNECTOR = 30;
export const RAIL_LABEL = 86;
export const RAIL_RIGHT = RAIL_CONNECTOR + RAIL_LABEL;
export const LEGEND_RIGHT = 14;
// The plot needs this much canvas to stay readable beside a rail.
export const RAIL_MIN_WIDTH = 520;
// Centre-to-centre distance between two labels: one line of 9.5px text.
export const LABEL_GAP = 14;
// z is clamped by the backend at 6; the axis keeps a little headroom so a
// clamped curve does not sit flush against the frame.
const MIN_AXIS = 2.4;
const AXIS_HEADROOM = 1.08;

export interface LabelAnchor {
  symbol: string;
  y: number;
}

export interface PlacedLabel extends LabelAnchor {
  labelY: number;
}

export interface XRayLayout {
  mode: 'rail' | 'legend';
  padRight: number;
  /** Top to bottom in rail mode; empty in legend mode. */
  labels: PlacedLabel[];
}

/**
 * Spread labels apart by the least total movement. Shifting each by its index
 * turns "at least `gap` apart" into "in order", which pooling adjacent violators
 * solves exactly. `fits` is false when the span cannot hold them at that gap.
 */
export function placeLabels(
  anchors: LabelAnchor[],
  minY: number,
  maxY: number,
  gap: number = LABEL_GAP
): { labels: PlacedLabel[]; fits: boolean } {
  const sorted = [...anchors].sort((a, b) => a.y - b.y || a.symbol.localeCompare(b.symbol));
  const last = sorted.length - 1;
  if (last * gap > maxY - minY) {
    return { labels: sorted.map((a) => ({ ...a, labelY: a.y })), fits: false };
  }

  const blocks: Array<{ total: number; size: number }> = [];
  sorted.forEach((anchor, i) => {
    let block = { total: anchor.y - i * gap, size: 1 };
    while (blocks.length) {
      const before = blocks[blocks.length - 1];
      if (before.total / before.size < block.total / block.size) break;
      blocks.pop();
      block = { total: block.total + before.total, size: block.size + before.size };
    }
    blocks.push(block);
  });

  const lowest = minY;
  const highest = maxY - last * gap;
  const labels: PlacedLabel[] = [];
  blocks.forEach((block) => {
    const level = Math.min(highest, Math.max(lowest, block.total / block.size));
    for (let k = 0; k < block.size; k += 1) {
      const i = labels.length;
      labels.push({ ...sorted[i], labelY: level + i * gap });
    }
  });
  return { labels, fits: true };
}

/** The shared axis half-range, so every curve is directly comparable. */
export function xrayPeak(series: XRaySeries[]): number {
  let peak = MIN_AXIS;
  series.forEach((s) =>
    s.points.forEach((p) => {
      peak = Math.max(peak, Math.abs(p.z));
    })
  );
  return peak * AXIS_HEADROOM;
}

export function xrayY(z: number, height: number, peak: number): number {
  const plotH = height - PAD.top - PAD.bottom;
  return PAD.top + plotH / 2 - z * (plotH / 2 / peak);
}

export function xrayLayout(series: XRaySeries[], width: number, height: number): XRayLayout {
  const legend: XRayLayout = { mode: 'legend', padRight: LEGEND_RIGHT, labels: [] };
  if (width < RAIL_MIN_WIDTH) return legend;
  const peak = xrayPeak(series);
  const anchors = series.map((s) => ({
    symbol: s.symbol,
    y: xrayY(s.points[s.points.length - 1]?.z ?? s.current_z, height, peak)
  }));
  const half = LABEL_GAP / 2;
  const placed = placeLabels(anchors, half, height - half);
  if (!placed.fits) return legend;
  return { mode: 'rail', padRight: RAIL_RIGHT, labels: placed.labels };
}
