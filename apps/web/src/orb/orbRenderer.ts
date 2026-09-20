/**
 * Canvas-2D renderer for the TARS orb. Custom implementation (no third-party orb code).
 *
 * The look of every OrbState is a small set of numeric targets that the renderer eases toward each
 * frame, so state changes morph smoothly instead of switching. Audio energy (already smoothed with
 * fast attack / slow decay) deforms the surface and drives the rings: fluid amplitude reaction, never a strobe.
 */
import { OrbState } from './orbState';

type RGB = [number, number, number];

export interface OrbLook {
  a: RGB; b: RGB; core: RGB;
  energy: number; // internal motion speed
  scale: number;  // base size
  glow: number;   // outer glow 0..1
  wobble: number; // resting surface deformation
  ring: number;   // resting ring visibility
  orbit: number;  // searching arc 0..1
  dim: number;    // overall opacity
  audio: number;  // how strongly audio energy is expressed
}

const look = (a: RGB, b: RGB, core: RGB, p: Partial<OrbLook> = {}): OrbLook => ({
  a, b, core, energy: 0.3, scale: 1, glow: 0.35, wobble: 0.02, ring: 0.08, orbit: 0, dim: 1, audio: 0, ...p,
});

export const LOOKS: Record<OrbState, OrbLook> = {
  IDLE: look([110, 150, 255], [22, 30, 76], [196, 214, 255], { energy: 0.16, scale: 0.94, glow: 0.22, wobble: 0.012, ring: 0.05 }),
  LISTENING: look([86, 196, 255], [16, 44, 92], [214, 240, 255], { energy: 0.42, scale: 1.04, glow: 0.38, wobble: 0.03, ring: 0.14, audio: 0.5 }),
  USER_SPEAKING: look([98, 232, 214], [12, 56, 84], [220, 255, 246], { energy: 0.7, scale: 1.06, glow: 0.5, wobble: 0.04, ring: 0.3, audio: 1 }),
  THINKING: look([150, 132, 255], [30, 22, 84], [226, 218, 255], { energy: 0.55, scale: 1.0, glow: 0.4, wobble: 0.05, ring: 0.1 }),
  TOOL_USE: look([92, 178, 255], [14, 40, 96], [214, 234, 255], { energy: 0.8, scale: 1.0, glow: 0.42, wobble: 0.03, ring: 0.12, orbit: 1 }),
  CLAUDE_DEEP_REASONING: look([182, 122, 255], [38, 18, 84], [236, 218, 255], { energy: 0.36, scale: 1.05, glow: 0.5, wobble: 0.07, ring: 0.22 }),
  ASSISTANT_SPEAKING: look([255, 208, 168], [70, 48, 110], [255, 244, 232], { energy: 0.6, scale: 1.06, glow: 0.55, wobble: 0.04, ring: 0.3, audio: 1 }),
  ALERT: look([255, 190, 92], [84, 52, 24], [255, 240, 208], { energy: 0.5, scale: 1.1, glow: 0.7, wobble: 0.03, ring: 0.55 }),
  ERROR: look([222, 112, 134], [64, 22, 36], [255, 218, 224], { energy: 0.12, scale: 0.96, glow: 0.3, wobble: 0.01, ring: 0.1, dim: 0.85 }),
  DISCONNECTED: look([122, 132, 152], [26, 30, 40], [190, 198, 214], { energy: 0.05, scale: 0.88, glow: 0.08, wobble: 0.005, ring: 0.02, dim: 0.42 }),
};

const lerp = (a: number, b: number, k: number) => a + (b - a) * k;
const lerpRGB = (a: RGB, b: RGB, k: number): RGB => [lerp(a[0], b[0], k), lerp(a[1], b[1], k), lerp(a[2], b[2], k)];
const rgba = (c: RGB, a: number) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${Math.max(0, Math.min(1, a)).toFixed(3)})`;

export interface FrameInput {
  t: number;        // seconds
  dt: number;       // seconds since previous frame
  state: OrbState;
  level: number;    // smoothed audio energy 0..1 for the current source
  flash: number;    // 0..1 success flash progress (0 = none)
  reduced: boolean; // prefers-reduced-motion: static, no wobble/rotation
}

export class OrbRenderer {
  private cur: OrbLook = { ...LOOKS.IDLE };
  private ripple = 0;
  private lastLevel = 0;

  constructor(private ctx: CanvasRenderingContext2D, private size: number) {}

  /** Current eased look (exposed for tests). */
  get look(): OrbLook {
    return this.cur;
  }

  snapTo(state: OrbState) {
    this.cur = { ...LOOKS[state] };
  }

  draw(f: FrameInput) {
    const { ctx, size } = this;
    const target = LOOKS[f.state];
    const k = 1 - Math.exp(-f.dt * 6.5);
    const c = this.cur;
    c.a = lerpRGB(c.a, target.a, k); c.b = lerpRGB(c.b, target.b, k); c.core = lerpRGB(c.core, target.core, k);
    for (const key of ['energy', 'scale', 'glow', 'wobble', 'ring', 'orbit', 'dim', 'audio'] as const) {
      c[key] = lerp(c[key], target[key], k);
    }
    const level = f.reduced ? 0 : f.level * c.audio;
    // Ripple: expands on rising energy, fades otherwise (soft, never a strobe).
    if (level - this.lastLevel > 0.06) this.ripple = Math.min(1, this.ripple + 0.5);
    this.lastLevel = level;
    this.ripple = Math.max(0, this.ripple - f.dt * 0.9);

    const cx = size / 2, cy = size / 2;
    const R = size * 0.27 * c.scale * (1 + level * 0.2);
    const t = f.reduced ? 0 : f.t * (0.35 + c.energy);
    ctx.clearRect(0, 0, size, size);
    ctx.globalAlpha = c.dim;

    // outer glow
    // keep the glow inside the canvas so it fades out instead of ending on a hard square edge
    const glowOuter = Math.min(R * (1.5 + level * 0.45), size / 2 - 1);
    const glow = ctx.createRadialGradient(cx, cy, Math.min(R * 0.7, glowOuter * 0.5), cx, cy, glowOuter);
    glow.addColorStop(0, rgba(c.a, c.glow * (0.6 + level * 0.5)));
    glow.addColorStop(1, rgba(c.a, 0));
    ctx.fillStyle = glow;
    ctx.beginPath(); ctx.arc(cx, cy, glowOuter, 0, Math.PI * 2); ctx.fill();

    // deformed body
    const wob = c.wobble + level * 0.1;
    const n = 72;
    ctx.beginPath();
    for (let i = 0; i <= n; i++) {
      const a = (i / n) * Math.PI * 2;
      const r = R * (1 + wob * (0.5 * Math.sin(3 * a + t * 1.1) + 0.32 * Math.sin(5 * a - t * 0.8) + 0.22 * Math.sin(2 * a + t * 0.6 + level * 4)));
      const x = cx + Math.cos(a) * r, y = cy + Math.sin(a) * r;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.closePath();
    const body = ctx.createRadialGradient(cx - R * 0.2, cy - R * 0.26, R * 0.05, cx, cy, R * 1.05);
    body.addColorStop(0, rgba(c.core, 0.66));
    body.addColorStop(0.45, rgba(c.a, 0.85));
    body.addColorStop(0.9, rgba(lerpRGB(c.a, c.b, 0.65), 0.88));
    body.addColorStop(1, rgba(c.a, 0.55)); // luminous rim instead of a dark edge
    ctx.fillStyle = body;
    ctx.fill();

    // inner drifting light (clipped to the body)
    ctx.save();
    ctx.clip();
    ctx.globalCompositeOperation = 'lighter';
    for (let i = 0; i < 3; i++) {
      const ang = t * (0.5 + i * 0.27) + i * 2.1;
      const rad = R * (0.28 + 0.12 * i + level * 0.12);
      const px = cx + Math.cos(ang) * rad, py = cy + Math.sin(ang * 1.13) * rad;
      const g = ctx.createRadialGradient(px, py, 0, px, py, R * (0.62 - i * 0.08));
      g.addColorStop(0, rgba(i === 1 ? c.core : c.a, 0.42 + level * 0.3));
      g.addColorStop(1, rgba(c.a, 0));
      ctx.fillStyle = g;
      ctx.fillRect(cx - R * 1.4, cy - R * 1.4, R * 2.8, R * 2.8);
    }
    ctx.restore();
    ctx.globalCompositeOperation = 'source-over';

    // soft highlight
    const hl = ctx.createRadialGradient(cx - R * 0.38, cy - R * 0.46, 0, cx - R * 0.38, cy - R * 0.46, R * 0.7);
    hl.addColorStop(0, 'rgba(255,255,255,0.08)');
    hl.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = hl;
    ctx.beginPath(); ctx.arc(cx, cy, R * 0.98, 0, Math.PI * 2); ctx.fill();

    // rings (resting + audio ripple)
    const ringAlpha = c.ring + this.ripple * 0.35 * (level > 0.02 ? 1 : 0.4);
    if (ringAlpha > 0.01) {
      ctx.lineWidth = 1.2;
      ctx.strokeStyle = rgba(c.core, ringAlpha * 0.7);
      ctx.beginPath(); ctx.arc(cx, cy, R * (1.2 + this.ripple * 0.28), 0, Math.PI * 2); ctx.stroke();
      ctx.strokeStyle = rgba(c.a, ringAlpha * 0.4);
      ctx.beginPath(); ctx.arc(cx, cy, R * (1.36 + this.ripple * 0.4), 0, Math.PI * 2); ctx.stroke();
    }

    // searching arc (tool use)
    if (c.orbit > 0.02) {
      const start = f.t * 2.4;
      ctx.lineWidth = 2;
      ctx.lineCap = 'round';
      ctx.strokeStyle = rgba(c.core, 0.75 * c.orbit);
      ctx.beginPath(); ctx.arc(cx, cy, R * 1.28, start, start + 1.1); ctx.stroke();
      ctx.strokeStyle = rgba(c.a, 0.4 * c.orbit);
      ctx.beginPath(); ctx.arc(cx, cy, R * 1.28, start + Math.PI, start + Math.PI + 0.5); ctx.stroke();
    }

    // success flash: one expanding thin ring
    if (f.flash > 0 && f.flash < 1) {
      ctx.lineWidth = 1.6;
      ctx.strokeStyle = rgba(c.core, (1 - f.flash) * 0.8);
      ctx.beginPath(); ctx.arc(cx, cy, R * (1.1 + f.flash * 0.9), 0, Math.PI * 2); ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }
}
