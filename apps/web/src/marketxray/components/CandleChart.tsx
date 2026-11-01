/**
 * Candlestick chart with order-block zones and structure levels.
 *
 * Zones are drawn at their real prices on the same scale as the candles, so a
 * rectangle's position on screen is the actual price band the backend
 * detected. A zone's border style encodes its lifecycle state: solid while it
 * is still live, dashed once it has been invalidated or expired.
 *
 * The price scale deliberately includes every visible zone's edges as well as
 * the candle highs and lows, so a zone can never be clipped out of view and
 * silently appear not to exist.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import type { Candle, OrderBlock } from '../types';
import { placeLabels } from './xrayLabels';

const PAD = { top: 14, right: 66, bottom: 22, left: 8 };
const ZONE_LABEL_GAP = 11;

interface Props {
  candles: Candle[];
  zones: OrderBlock[];
  precision: number;
  symbol: string;
  showInvalidated: boolean;
}

interface Readout {
  x: number;
  y: number;
  candle: Candle;
}

const LIVE_STATES = new Set(['FRESH', 'TAPPED', 'MITIGATED']);

export const CandleChart: React.FC<Props> = ({
  candles,
  zones,
  precision,
  symbol,
  showInvalidated
}) => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const pointerRef = useRef<{ x: number; y: number } | null>(null);
  const [readout, setReadout] = useState<Readout | null>(null);

  const draw = useCallback(() => {
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

    const plotW = cssW - PAD.left - PAD.right;
    const plotH = cssH - PAD.top - PAD.bottom;

    ctx.fillStyle = '#060a13';
    ctx.fillRect(PAD.left, PAD.top, plotW, plotH);

    if (!candles.length) {
      ctx.fillStyle = '#55657f';
      ctx.font = '12px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('No candles at this moment.', PAD.left + plotW / 2, PAD.top + plotH / 2);
      return;
    }

    const visible = zones.filter((z) => showInvalidated || LIVE_STATES.has(z.status));

    // Scale spans candles and every drawn zone, so nothing is clipped away.
    let lo = Math.min(...candles.map((c) => c.low));
    let hi = Math.max(...candles.map((c) => c.high));
    visible.forEach((z) => {
      lo = Math.min(lo, z.zone_low);
      hi = Math.max(hi, z.zone_high);
    });
    const span = hi - lo || 1;
    lo -= span * 0.04;
    hi += span * 0.04;

    const yAt = (price: number): number => PAD.top + ((hi - price) / (hi - lo)) * plotH;
    const slot = plotW / candles.length;
    const bodyW = Math.max(1, Math.min(slot * 0.68, 11));
    const xAt = (i: number): number => PAD.left + i * slot + slot / 2;

    const lastClose = candles[candles.length - 1].close;

    // --- price gridlines ---
    ctx.font = '9px ui-monospace, monospace';
    for (let k = 0; k <= 4; k += 1) {
      const price = lo + ((hi - lo) * k) / 4;
      const y = yAt(price);
      ctx.strokeStyle = 'rgba(34, 48, 82, 0.4)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(PAD.left, y);
      ctx.lineTo(PAD.left + plotW, y);
      ctx.stroke();
      ctx.fillStyle = '#44536c';
      ctx.textAlign = 'left';
      // The last-price tag owns its row of the axis.
      if (Math.abs(y - yAt(lastClose)) > 12) {
        ctx.fillText(price.toFixed(precision), PAD.left + plotW + 6, y + 3);
      }
    }

    // --- order-block zones, behind the candles ---
    const firstTs = candles[0].ts;
    visible.forEach((z) => {
      const top = yAt(z.zone_high);
      const bottom = yAt(z.zone_low);
      const height = Math.max(1.5, bottom - top);
      // Start the rectangle at the origin bar when it is on screen; a zone
      // formed before the window starts is drawn from the left edge.
      const originIdx = candles.findIndex((c) => c.ts >= z.origin_ts);
      const startX = z.origin_ts < firstTs || originIdx < 0 ? PAD.left : xAt(originIdx) - bodyW / 2;
      const live = LIVE_STATES.has(z.status);
      const up = z.side === 'BULLISH';
      const hue = up ? '25, 230, 160' : '255, 77, 126';

      ctx.fillStyle = `rgba(${hue}, ${live ? 0.13 : 0.05})`;
      ctx.fillRect(startX, top, PAD.left + plotW - startX, height);

      ctx.strokeStyle = `rgba(${hue}, ${live ? 0.62 : 0.26})`;
      ctx.lineWidth = 1;
      ctx.setLineDash(live ? [] : [4, 3]);
      ctx.strokeRect(startX, top, PAD.left + plotW - startX, height);
      ctx.setLineDash([]);

      // Break-of-structure level that qualified the zone.
      const bosY = yAt(z.bos_level);
      if (bosY > PAD.top && bosY < PAD.top + plotH && live) {
        ctx.strokeStyle = `rgba(${hue}, 0.3)`;
        ctx.setLineDash([2, 4]);
        ctx.beginPath();
        ctx.moveTo(startX, bosY);
        ctx.lineTo(PAD.left + plotW, bosY);
        ctx.stroke();
        ctx.setLineDash([]);
      }
    });

    // --- candles ---
    candles.forEach((c, i) => {
      const up = c.close >= c.open;
      const colour = up ? '#19e6a0' : '#ff4d7e';
      const x = xAt(i);

      ctx.strokeStyle = colour;
      ctx.globalAlpha = 0.72;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, yAt(c.high));
      ctx.lineTo(x, yAt(c.low));
      ctx.stroke();
      ctx.globalAlpha = 1;

      const yOpen = yAt(c.open);
      const yClose = yAt(c.close);
      const top = Math.min(yOpen, yClose);
      const h = Math.max(1, Math.abs(yClose - yOpen));
      ctx.fillStyle = colour;
      ctx.fillRect(x - bodyW / 2, top, bodyW, h);
    });

    // --- zone labels: side, state and age, above the candles and clear of each
    // other. The table below lists every zone if the plot cannot hold them all.
    const zoneAnchor = (z: OrderBlock) => ({
      symbol: z.zone_id,
      y: (yAt(z.zone_high) + yAt(z.zone_low)) / 2
    });
    const zoneLabels = (rows: OrderBlock[]) =>
      placeLabels(rows.map(zoneAnchor), PAD.top + 6, PAD.top + plotH - 6, ZONE_LABEL_GAP);
    const everyZone = zoneLabels(visible);
    const placedZones = everyZone.fits
      ? everyZone.labels
      : zoneLabels(visible.filter((z) => LIVE_STATES.has(z.status))).labels;
    const zoneById = new Map(visible.map((z) => [z.zone_id, z]));
    ctx.font = '8.5px ui-monospace, monospace';
    ctx.textAlign = 'right';
    placedZones.forEach((placed) => {
      const z = zoneById.get(placed.symbol);
      if (!z) return;
      const up = z.side === 'BULLISH';
      const hue = up ? '25, 230, 160' : '255, 77, 126';
      const label = `${up ? 'OB+' : 'OB-'} ${z.status[0]}${z.status.slice(1, 4).toLowerCase()} ${z.age_bars}b`;
      const right = PAD.left + plotW - 4;
      ctx.fillStyle = 'rgba(6, 10, 19, 0.86)';
      ctx.fillRect(right - ctx.measureText(label).width - 3, placed.labelY - 5, ctx.measureText(label).width + 6, 10);
      ctx.fillStyle = `rgba(${hue}, ${LIVE_STATES.has(z.status) ? 0.95 : 0.6})`;
      ctx.fillText(label, right, placed.labelY + 3);
    });

    // --- last price marker ---
    const last = candles[candles.length - 1];
    const lastY = yAt(last.close);
    ctx.strokeStyle = 'rgba(219, 230, 245, 0.4)';
    ctx.setLineDash([3, 3]);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(PAD.left, lastY);
    ctx.lineTo(PAD.left + plotW, lastY);
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.fillStyle = last.close >= last.open ? '#19e6a0' : '#ff4d7e';
    ctx.fillRect(PAD.left + plotW + 2, lastY - 7, PAD.right - 4, 14);
    ctx.fillStyle = '#04070e';
    ctx.font = '600 9px ui-monospace, monospace';
    ctx.textAlign = 'left';
    ctx.fillText(last.close.toFixed(precision), PAD.left + plotW + 6, lastY + 3);

    // --- crosshair + OHLC readout ---
    const pointer = pointerRef.current;
    if (pointer && pointer.x > PAD.left && pointer.x < PAD.left + plotW) {
      const idx = Math.min(candles.length - 1, Math.max(0, Math.floor((pointer.x - PAD.left) / slot)));
      const c = candles[idx];
      const cx = xAt(idx);
      ctx.strokeStyle = 'rgba(219, 230, 245, 0.2)';
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(cx, PAD.top);
      ctx.lineTo(cx, PAD.top + plotH);
      ctx.stroke();
      ctx.setLineDash([]);
      setReadout({ x: cx, y: pointer.y, candle: c });
    } else {
      setReadout(null);
    }

    // --- time axis ---
    ctx.fillStyle = '#44536c';
    ctx.font = '9px ui-monospace, monospace';
    ctx.textAlign = 'left';
    ctx.fillText(candles[0].ts.slice(0, 16).replace('T', ' '), PAD.left, cssH - 7);
    ctx.textAlign = 'right';
    ctx.fillText(`${last.ts.slice(0, 16).replace('T', ' ')} UTC`, PAD.left + plotW, cssH - 7);
    // Dropped before it can run into the dates either side of it.
    if (plotW >= 420) {
      ctx.textAlign = 'center';
      ctx.fillText(`${symbol} · ${candles.length} completed bars`, PAD.left + plotW / 2, cssH - 7);
    }
  }, [candles, zones, precision, symbol, showInvalidated]);

  useEffect(() => {
    let raf = 0;
    const tick = () => {
      draw();
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [draw]);

  return (
    <div style={{ position: 'relative' }} ref={wrapRef}>
      <canvas
        ref={canvasRef}
        className="mx-chart-canvas"
        onMouseMove={(e) => {
          const rect = e.currentTarget.getBoundingClientRect();
          pointerRef.current = { x: e.clientX - rect.left, y: e.clientY - rect.top };
        }}
        onMouseLeave={() => {
          pointerRef.current = null;
        }}
        role="img"
        aria-label={`${symbol} candlestick chart with order-block zones`}
      />
      {readout && (
        <div
          className="mx-tooltip"
          style={{
            left: Math.min(Math.max(readout.x + 12, 8), (wrapRef.current?.clientWidth ?? 600) - 180),
            top: 10
          }}
        >
          <div className="mx-tooltip-head">
            <span>{readout.candle.ts.slice(0, 16).replace('T', ' ')}</span>
            <span className={readout.candle.close >= readout.candle.open ? 'mx-up' : 'mx-down'}>
              {readout.candle.close >= readout.candle.open ? '▲' : '▼'}
            </span>
          </div>
          {(['open', 'high', 'low', 'close'] as const).map((k) => (
            <div className="mx-tooltip-row" key={k}>
              <span>{k[0].toUpperCase() + k.slice(1)}</span>
              <b>{readout.candle[k].toFixed(precision)}</b>
            </div>
          ))}
          <div className="mx-tooltip-row">
            <span>Spread</span>
            <b>{readout.candle.spread.toFixed(Math.min(precision + 1, 6))}</b>
          </div>
        </div>
      )}
    </div>
  );
};
