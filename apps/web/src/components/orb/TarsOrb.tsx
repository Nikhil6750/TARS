import React, { useEffect, useRef } from 'react';
import { audioLevels, shapeLevel, smoothLevel } from '../../orb/audioLevels';
import { OrbState, levelSource } from '../../orb/orbState';
import { OrbRenderer } from '../../orb/orbRenderer';

/** Smoothed level of the active source, shared with the waveform (read-only, outside React). */
export const orbLevel = { value: 0 };

const QUIET: ReadonlySet<OrbState> = new Set<OrbState>(['IDLE', 'DISCONNECTED', 'ERROR']);

export function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

interface Props {
  state: OrbState;
  flashUntil: number;
  /** Canvas edge in CSS px; the visible orb body is about 54% of this. */
  size?: number;
}

export const TarsOrb: React.FC<Props> = ({ state, flashUntil, size = 168 }) => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const stateRef = useRef(state);
  const flashRef = useRef(flashUntil);
  const redraw = useRef<(() => void) | null>(null);
  stateRef.current = state;
  flashRef.current = flashUntil;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    ctx.scale(dpr, dpr);
    const renderer = new OrbRenderer(ctx, size);
    renderer.snapTo(stateRef.current);
    const reduced = prefersReducedMotion();
    let raf = 0;
    let last = performance.now();
    let level = 0;
    let alive = true;

    const frame = (now: number, force = false) => {
      const st = stateRef.current;
      const dt = Math.min(0.1, (now - last) / 1000);
      // Idle presence needs ~20 fps, not 60: the loop is the only recurring cost of a 24/7 app.
      if (!force && QUIET.has(st) && now - last < 48) {
        raf = requestAnimationFrame(frame);
        return;
      }
      last = now;
      const src = levelSource(st);
      const raw = src === 'mic' ? audioLevels.mic(now) : src === 'assistant' ? audioLevels.assistant() : 0;
      level = smoothLevel(level, shapeLevel(raw), dt * 1000);
      orbLevel.value = level;
      const flash = flashRef.current > Date.now() ? 1 - (flashRef.current - Date.now()) / 1100 : 0;
      renderer.draw({ t: now / 1000, dt: Math.max(dt, 0.001), state: st, level, flash, reduced });
      if (alive && !reduced && !document.hidden) raf = requestAnimationFrame(frame);
    };

    redraw.current = () => {
      renderer.snapTo(stateRef.current);
      frame(performance.now(), true);
    };
    const onVisibility = () => {
      cancelAnimationFrame(raf);
      last = performance.now();
      if (!document.hidden && !reduced) raf = requestAnimationFrame(frame);
    };
    document.addEventListener('visibilitychange', onVisibility);
    if (reduced) frame(performance.now(), true);
    else raf = requestAnimationFrame(frame);
    return () => {
      alive = false;
      cancelAnimationFrame(raf);
      document.removeEventListener('visibilitychange', onVisibility);
      redraw.current = null;
    };
  }, [size]);

  // Reduced motion draws no loop, so repaint once per state change.
  useEffect(() => {
    if (prefersReducedMotion()) redraw.current?.();
  }, [state]);

  return (
    <canvas
      ref={canvasRef}
      data-testid="tars-orb-canvas"
      data-orb-state={state}
      aria-hidden="true"
      style={{ width: size, height: size, display: 'block', pointerEvents: 'none' }}
    />
  );
};
