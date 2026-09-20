import React, { useEffect, useRef, useState } from 'react';
import { WAVEFORM_STATES, OrbState } from '../../orb/orbState';
import { orbLevel, prefersReducedMotion } from './TarsOrb';

const BARS = 26;
const W = 104;
const H = 22;

const COLOR: Partial<Record<OrbState, string>> = {
  LISTENING: '150,214,255',
  USER_SPEAKING: '150,240,224',
  ASSISTANT_SPEAKING: '255,226,196',
};

/**
 * Small waveform under the orb. Rendered ONLY while voice is active (LISTENING / USER_SPEAKING /
 * ASSISTANT_SPEAKING): it fades in, follows the real smoothed audio level, then fades out and
 * its animation loop stops completely.
 */
export const VoiceBar: React.FC<{ state: OrbState }> = ({ state }) => {
  const active = WAVEFORM_STATES.has(state);
  const [mounted, setMounted] = useState(active);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const stateRef = useRef(state);
  stateRef.current = state;

  useEffect(() => {
    if (active) {
      setMounted(true);
      return;
    }
    const t = setTimeout(() => setMounted(false), 350);
    return () => clearTimeout(t);
  }, [active]);

  useEffect(() => {
    if (!mounted) return;
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext('2d');
    if (!canvas || !ctx) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    ctx.scale(dpr, dpr);
    const history = new Array<number>(BARS).fill(0);
    const reduced = prefersReducedMotion();
    let raf = 0;
    let lastSample = 0;
    const frame = (now: number) => {
      if (now - lastSample > 40) {
        history.push(orbLevel.value);
        history.shift();
        lastSample = now;
      }
      ctx.clearRect(0, 0, W, H);
      const rgb = COLOR[stateRef.current] ?? '180,200,255';
      const gap = W / BARS;
      for (let i = 0; i < BARS; i++) {
        const edge = Math.sin((i / (BARS - 1)) * Math.PI); // taper the ends
        const v = reduced ? 0.18 : history[i];
        const h = Math.max(2, (0.12 + v * 0.95) * H * (0.35 + 0.65 * edge));
        ctx.fillStyle = `rgba(${rgb},${(0.32 + 0.5 * edge).toFixed(2)})`;
        ctx.beginPath();
        ctx.roundRect(i * gap + gap * 0.22, (H - h) / 2, gap * 0.56, h, 1.5);
        ctx.fill();
      }
      if (stateRef.current && WAVEFORM_STATES.has(stateRef.current) && !document.hidden) raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [mounted]);

  if (!mounted) return null;
  return (
    <canvas
      ref={canvasRef}
      data-testid="orb-waveform"
      aria-hidden="true"
      style={{ width: W, height: H, opacity: active ? 0.9 : 0, transition: 'opacity 300ms ease', pointerEvents: 'none' }}
    />
  );
};
