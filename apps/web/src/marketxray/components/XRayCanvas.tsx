/**
 * The Market X-Ray — the dashboard's primary visual.
 *
 * Each instrument is one luminous curve of its volatility-normalised return
 * (`z`) across the last N completed bars. Curves above the zero axis are
 * instruments that rose over the trailing window; below, those that fell.
 * Vertical distance is the size of the move relative to that instrument's own
 * typical movement, which is what lets gold and EURUSD share an axis.
 *
 * **Every plotted coordinate is a real observation.** A point's y comes from a
 * `z` the backend computed from recorded bid/ask candles, and its x is that
 * bar's position in time. Two things are animated, and neither invents data:
 *
 * 1. On a data change, each curve eases from its previous real shape to its
 *    new real shape over ~600ms. Both endpoints are measured states; the
 *    in-between frames are a visual transition, not observations, and they
 *    are never read back or reported as values.
 * 2. A particle rides each curve. Its position is interpolated *between two
 *    adjacent real observations*, so it only ever travels over ground the
 *    data actually covers.
 *
 * There is no random motion anywhere in this component.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { XRaySeries, XRaySnapshot } from '../types';
import { sourceName } from './TopNav';
import { PAD, RAIL_CONNECTOR, xrayLayout, xrayPeak, xrayY } from './xrayLabels';

const TRANSITION_MS = 600;
const PARTICLE_PERIOD_MS = 5200;
// The tooltip is sized in CSS; these keep it inside the plot on every side.
const TOOLTIP_WIDTH = 232;
const TOOLTIP_HEIGHT = 150;

interface Hover {
  symbol: string;
  x: number;
  y: number;
  z: number;
  close: number;
  pct: number;
  ts: string;
}

interface Props {
  snapshot: XRaySnapshot | null;
  selected: string | null;
  onSelect: (symbol: string) => void;
  loading?: boolean;
}

/** Catmull-Rom through the points, emitted as bezier segments. */
function strokeSmooth(ctx: CanvasRenderingContext2D, pts: Array<[number, number]>): void {
  if (pts.length < 2) return;
  ctx.beginPath();
  ctx.moveTo(pts[0][0], pts[0][1]);
  if (pts.length === 2) {
    ctx.lineTo(pts[1][0], pts[1][1]);
    return;
  }
  for (let i = 0; i < pts.length - 1; i += 1) {
    const p0 = pts[i === 0 ? 0 : i - 1];
    const p1 = pts[i];
    const p2 = pts[i + 1];
    const p3 = pts[i + 2 < pts.length ? i + 2 : i + 1];
    ctx.bezierCurveTo(
      p1[0] + (p2[0] - p0[0]) / 6,
      p1[1] + (p2[1] - p0[1]) / 6,
      p2[0] - (p3[0] - p1[0]) / 6,
      p2[1] - (p3[1] - p1[1]) / 6,
      p2[0],
      p2[1]
    );
  }
  ctx.stroke();
}

const easeInOut = (t: number): number =>
  t < 0.5 ? 2 * t * t : 1 - (1 - t) * (1 - t) * 2;

export const XRayCanvas: React.FC<Props> = ({ snapshot, selected, onSelect, loading }) => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<Hover | null>(null);

  // Previous real z-series per symbol, kept only to ease between two measured
  // states. Never used as a value, only as a transition start.
  const prevRef = useRef<Map<string, number[]>>(new Map());
  const transitionStart = useRef<number>(0);
  const pointerRef = useRef<{ x: number; y: number } | null>(null);
  // Screen-space geometry of the last painted frame, for hit testing.
  const geomRef = useRef<Map<string, Array<[number, number]>>>(new Map());
  const labelHoverRef = useRef<string | null>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });

  const series = useMemo<XRaySeries[]>(() => snapshot?.series ?? [], [snapshot]);
  // Follows the real end values, not the eased frames between two snapshots.
  const layout = useMemo(() => xrayLayout(series, size.width, size.height), [series, size]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const measure = () => {
      const { clientWidth: width, clientHeight: height } = canvas;
      setSize((current) =>
        current.width === width && current.height === height ? current : { width, height }
      );
    };
    measure();
    window.addEventListener('resize', measure);
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    observer?.observe(canvas);
    return () => {
      window.removeEventListener('resize', measure);
      observer?.disconnect();
    };
  }, []);

  // A new snapshot starts a transition from whatever was last painted.
  useEffect(() => {
    if (!snapshot) return;
    transitionStart.current = performance.now();
    return () => {
      const next = new Map<string, number[]>();
      snapshot.series.forEach((s) => next.set(s.symbol, s.points.map((p) => p.z)));
      prevRef.current = next;
    };
  }, [snapshot]);

  const draw = useCallback(
    (now: number) => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      const ctx = canvas.getContext('2d');
      if (!ctx) return;

      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const cssW = canvas.clientWidth;
      const cssH = canvas.clientHeight;
      if (canvas.width !== cssW * dpr || canvas.height !== cssH * dpr) {
        canvas.width = Math.round(cssW * dpr);
        canvas.height = Math.round(cssH * dpr);
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, cssW, cssH);

      const plotW = cssW - PAD.left - layout.padRight;
      const plotH = cssH - PAD.top - PAD.bottom;
      const midY = PAD.top + plotH / 2;

      // Backdrop
      ctx.fillStyle = '#060a13';
      ctx.fillRect(PAD.left, PAD.top, plotW, plotH);

      if (!series.length) {
        ctx.fillStyle = '#55657f';
        ctx.font = '12px system-ui, sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText(
          loading ? 'Computing X-Ray…' : 'No instruments with enough history at this moment.',
          PAD.left + plotW / 2,
          midY
        );
        return;
      }

      const maxPoints = Math.max(...series.map((s) => s.points.length));
      const progress = Math.min(1, (now - transitionStart.current) / TRANSITION_MS);
      const eased = easeInOut(progress);

      // Shared symmetric axis so every curve is directly comparable.
      const peak = xrayPeak(series);

      const xAt = (i: number, len: number): number =>
        PAD.left + (len <= 1 ? plotW : (i / (len - 1)) * plotW);
      const yAt = (z: number): number => xrayY(z, cssH, peak);

      // --- grid + zero reference line ---
      ctx.lineWidth = 1;
      const steps = 4;
      for (let k = -steps; k <= steps; k += 1) {
        if (k === 0) continue;
        const z = (peak * k) / steps;
        const y = yAt(z);
        ctx.strokeStyle = 'rgba(34, 48, 82, 0.4)';
        ctx.beginPath();
        ctx.moveTo(PAD.left, y);
        ctx.lineTo(PAD.left + plotW, y);
        ctx.stroke();
        ctx.fillStyle = '#44536c';
        ctx.font = '9px ui-monospace, monospace';
        ctx.textAlign = 'right';
        ctx.fillText(z > 0 ? `+${z.toFixed(1)}` : z.toFixed(1), PAD.left - 7, y + 3);
      }

      // Zero line: deliberately the brightest rule on the plot.
      ctx.strokeStyle = 'rgba(160, 185, 215, 0.55)';
      ctx.lineWidth = 1.25;
      ctx.beginPath();
      ctx.moveTo(PAD.left, midY);
      ctx.lineTo(PAD.left + plotW, midY);
      ctx.stroke();
      ctx.fillStyle = '#8296b4';
      ctx.font = '600 9px ui-monospace, monospace';
      ctx.textAlign = 'right';
      ctx.fillText('0.0', PAD.left - 7, midY + 3);

      // --- curves ---
      const pointer = pointerRef.current;
      let best: { symbol: string; dist: number; idx: number } | null = null;
      const geom = new Map<string, Array<[number, number]>>();

      // Weakest first so the strongest movers sit on top.
      const ordered = [...series].sort((a, b) => Math.abs(a.current_z) - Math.abs(b.current_z));

      ordered.forEach((s) => {
        const prev = prevRef.current.get(s.symbol);
        const len = s.points.length;
        const pts: Array<[number, number]> = s.points.map((p, i) => {
          // Ease from the previous measured z to this one. Both ends are real.
          const from = prev && prev.length === len ? prev[i] : p.z;
          const z = from + (p.z - from) * eased;
          return [xAt(i, len), yAt(z)];
        });
        geom.set(s.symbol, pts);

        if (pointer) {
          pts.forEach(([px, py], i) => {
            const d = Math.hypot(px - pointer.x, py - pointer.y);
            if (!best || d < best.dist) best = { symbol: s.symbol, dist: d, idx: i };
          });
        }
      });
      geomRef.current = geom;

      const labelled = labelHoverRef.current ? geom.get(labelHoverRef.current) : undefined;
      if (labelHoverRef.current && labelled?.length) {
        best = { symbol: labelHoverRef.current, dist: 0, idx: labelled.length - 1 };
      }

      const hoveredSymbol =
        best && (best as { dist: number }).dist < 26 ? (best as { symbol: string }).symbol : null;

      ordered.forEach((s) => {
        const pts = geom.get(s.symbol);
        if (!pts || pts.length < 2) return;

        const up = s.current_z >= 0;
        const emphasis = s.symbol === selected || s.symbol === hoveredSymbol;
        const magnitude = Math.min(Math.abs(s.current_z) / 3, 1);
        const alpha = emphasis ? 0.98 : 0.3 + magnitude * 0.42;
        const hue = up ? '25, 230, 160' : '255, 77, 126';

        ctx.save();
        ctx.lineJoin = 'round';
        ctx.lineCap = 'round';
        // The glow is what gives the plot its luminous quality.
        ctx.shadowColor = `rgba(${hue}, ${emphasis ? 0.75 : 0.35})`;
        ctx.shadowBlur = emphasis ? 13 : 5 + magnitude * 5;
        ctx.strokeStyle = `rgba(${hue}, ${alpha})`;
        ctx.lineWidth = emphasis ? 2.1 : 1.05;
        strokeSmooth(ctx, pts);
        ctx.restore();

        // Particle: rides between two adjacent real observations.
        const phase = ((now % PARTICLE_PERIOD_MS) / PARTICLE_PERIOD_MS + s.rank * 0.055) % 1;
        const exact = phase * (pts.length - 1);
        const i0 = Math.floor(exact);
        const i1 = Math.min(pts.length - 1, i0 + 1);
        const frac = exact - i0;
        const px = pts[i0][0] + (pts[i1][0] - pts[i0][0]) * frac;
        const py = pts[i0][1] + (pts[i1][1] - pts[i0][1]) * frac;

        ctx.save();
        ctx.shadowColor = `rgba(${hue}, 0.9)`;
        ctx.shadowBlur = emphasis ? 12 : 7;
        ctx.fillStyle = `rgba(${hue}, ${emphasis ? 1 : 0.8})`;
        ctx.beginPath();
        ctx.arc(px, py, emphasis ? 2.9 : 1.9, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();

        // End marker at the current (most recent) observation. Its label is a
        // real button in the rail, tied back to this point by a connector.
        const [ex, ey] = pts[pts.length - 1];
        ctx.save();
        ctx.shadowColor = `rgba(${hue}, 0.9)`;
        ctx.shadowBlur = emphasis ? 11 : 6;
        ctx.fillStyle = `rgba(${hue}, 1)`;
        ctx.beginPath();
        ctx.arc(ex, ey, emphasis ? 3.4 : 2.3, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();

        const placed = layout.labels.find((label) => label.symbol === s.symbol);
        if (placed) {
          ctx.strokeStyle = `rgba(${hue}, ${emphasis ? 0.95 : 0.4})`;
          ctx.lineWidth = emphasis ? 1.25 : 1;
          ctx.beginPath();
          ctx.moveTo(ex, ey);
          ctx.lineTo(ex + 8, ey);
          ctx.lineTo(ex + RAIL_CONNECTOR - 8, placed.labelY);
          ctx.lineTo(ex + RAIL_CONNECTOR - 2, placed.labelY);
          ctx.stroke();
        }
      });

      // --- hover readout ---
      if (hoveredSymbol && best) {
        const hit = best as { symbol: string; idx: number };
        const s = series.find((x) => x.symbol === hit.symbol);
        const pts = geom.get(hit.symbol);
        const point = s?.points[hit.idx];
        if (s && pts && point) {
          const [hx, hy] = pts[hit.idx];
          ctx.strokeStyle = 'rgba(219, 230, 245, 0.22)';
          ctx.lineWidth = 1;
          ctx.setLineDash([3, 3]);
          ctx.beginPath();
          ctx.moveTo(hx, PAD.top);
          ctx.lineTo(hx, PAD.top + plotH);
          ctx.stroke();
          ctx.setLineDash([]);

          ctx.fillStyle = '#ffffff';
          ctx.beginPath();
          ctx.arc(hx, hy, 3.6, 0, Math.PI * 2);
          ctx.fill();

          const wrap = wrapRef.current;
          setHover({
            symbol: s.symbol,
            x: hx,
            y: hy,
            z: point.z,
            close: point.close,
            pct: point.pct_change,
            ts: point.ts
          });
          if (wrap) wrap.style.cursor = 'pointer';
        }
      } else {
        setHover(null);
        if (wrapRef.current) wrapRef.current.style.cursor = 'crosshair';
      }

      // --- axis captions ---
      ctx.fillStyle = '#44536c';
      ctx.font = '9px ui-monospace, monospace';
      ctx.textAlign = 'left';
      ctx.fillText(`${maxPoints} bars ago`, PAD.left, cssH - 8);
      ctx.textAlign = 'right';
      ctx.fillText('now', PAD.left + plotW, cssH - 8);
      // Dropped before it can run into the captions either side of it.
      if (plotW >= 320) {
        ctx.textAlign = 'center';
        ctx.fillText(
          `normalised return · ${snapshot?.timeframe ?? ''} · window ${snapshot?.return_window ?? ''} bars`,
          PAD.left + plotW / 2,
          cssH - 8
        );
      }

      ctx.save();
      ctx.translate(13, midY);
      ctx.rotate(-Math.PI / 2);
      ctx.textAlign = 'center';
      ctx.fillStyle = '#3c4a60';
      ctx.font = '9px ui-monospace, monospace';
      ctx.fillText('← bearish        bullish →', 0, 0);
      ctx.restore();
    },
    [series, selected, loading, snapshot, layout]
  );

  // Single rAF loop drives the transition and the particles.
  useEffect(() => {
    let raf = 0;
    const tick = (now: number) => {
      draw(now);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [draw]);

  const handleMove = (event: React.MouseEvent<HTMLCanvasElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    pointerRef.current = { x: event.clientX - rect.left, y: event.clientY - rect.top };
  };

  const handleLeave = () => {
    pointerRef.current = null;
  };

  const handleClick = () => {
    if (hover) onSelect(hover.symbol);
  };

  const tooltipStyle: React.CSSProperties = hover
    ? {
        left: Math.max(6, Math.min(hover.x + 14, size.width - TOOLTIP_WIDTH)),
        top: Math.max(6, Math.min(hover.y - 62, size.height - TOOLTIP_HEIGHT))
      }
    : {};

  const label = (s: XRaySeries, style?: React.CSSProperties) => {
    const value = `${s.current_z >= 0 ? '+' : ''}${s.current_z.toFixed(2)}`;
    return (
      <button
        key={s.symbol}
        type="button"
        className="mx-xray-label"
        data-symbol={s.symbol}
        data-dir={s.current_z >= 0 ? 'up' : 'down'}
        aria-pressed={s.symbol === selected}
        aria-label={`${s.symbol} ${value} sigma`}
        style={style}
        onClick={() => onSelect(s.symbol)}
        onMouseEnter={() => {
          labelHoverRef.current = s.symbol;
        }}
        onMouseLeave={() => {
          labelHoverRef.current = null;
        }}
        onFocus={() => {
          labelHoverRef.current = s.symbol;
        }}
        onBlur={() => {
          labelHoverRef.current = null;
        }}
      >
        <span>{s.symbol}</span>
        <b>{value}σ</b>
      </button>
    );
  };

  const bySymbol = new Map(series.map((s) => [s.symbol, s]));

  return (
    <div className="mx-xray-wrap" ref={wrapRef}>
      {layout.mode === 'rail' && (
        <div
          className="mx-xray-rail"
          role="group"
          aria-label="Instruments"
          style={{ left: size.width - layout.padRight + RAIL_CONNECTOR }}
        >
          {layout.labels.map((placed) => {
            const s = bySymbol.get(placed.symbol);
            return s ? label(s, { top: placed.labelY }) : null;
          })}
        </div>
      )}
      <canvas
        ref={canvasRef}
        className="mx-xray-canvas"
        onMouseMove={handleMove}
        onMouseLeave={handleLeave}
        onClick={handleClick}
        role="img"
        aria-label="Market X-Ray: volatility-normalised returns per instrument"
      />
      {hover && (
        <div className="mx-tooltip" style={tooltipStyle}>
          <div className="mx-tooltip-head">
            <span>{hover.symbol}</span>
            <span className={hover.z >= 0 ? 'mx-up' : 'mx-down'}>
              {hover.z >= 0 ? '+' : ''}
              {hover.z.toFixed(2)}σ
            </span>
          </div>
          <div className="mx-tooltip-row">
            <span>Close</span>
            <b>{hover.close.toPrecision(7)}</b>
          </div>
          <div className="mx-tooltip-row">
            <span>Window change</span>
            <b className={hover.pct >= 0 ? 'mx-up' : 'mx-down'}>
              {hover.pct >= 0 ? '+' : ''}
              {hover.pct.toFixed(3)}%
            </b>
          </div>
          <div className="mx-tooltip-row">
            <span>Bar (UTC)</span>
            <b>{hover.ts.slice(0, 16).replace('T', ' ')}</b>
          </div>
          <div className="mx-tooltip-row">
            <span>Timeframe</span>
            <b>{snapshot?.timeframe}</b>
          </div>
          <div className="mx-tooltip-foot">
            Source: {sourceName(snapshot?.provenance) ?? 'recorded'} bid/ask mid candles · price
            displacement, not money flow
          </div>
        </div>
      )}
      {layout.mode === 'legend' && series.length > 0 && (
        <div className="mx-xray-legend" role="group" aria-label="Instruments">
          {series.map((s) => label(s))}
        </div>
      )}
    </div>
  );
};
