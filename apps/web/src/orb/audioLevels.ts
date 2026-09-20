/**
 * Real audio energy for the orb, kept OUTSIDE React so 30-60 Hz updates never rerender the tree.
 *
 * mic       <- native microphone frames (tars://wake-audio-level, one per 32 ms frame)
 * assistant <- AnalyserNode on the assistant's playing PCM (Gemini) via a registered reader
 *
 * The renderer reads smoothed values each animation frame: fast attack, slower decay.
 */
type Reader = () => number;

let micRaw = 0;
let micAt = 0;
let assistantReader: Reader | null = null;
let assistantFallback = 0;

export const audioLevels = {
  pushMic(level: number, now = performance.now()) {
    micRaw = Math.max(0, Math.min(1, level));
    micAt = now;
  },
  /** Raw mic level, decayed to 0 if frames stop arriving (mic muted / disconnected). */
  mic(now = performance.now()): number {
    return now - micAt > 250 ? 0 : micRaw;
  },
  setAssistantReader(reader: Reader | null) {
    assistantReader = reader;
  },
  /** Only used when no amplitude reader exists (local-fallback WAV playback). */
  setAssistantFallback(level: number) {
    assistantFallback = level;
  },
  assistant(): number {
    if (assistantReader) {
      const v = assistantReader();
      if (v > 0) return v;
    }
    return assistantFallback;
  },
};

/** Fast attack, slower decay smoothing, frame-rate independent. */
export function smoothLevel(current: number, target: number, dtMs: number, attackMs = 45, decayMs = 220): number {
  const tau = target > current ? attackMs : decayMs;
  const k = 1 - Math.exp(-dtMs / tau);
  return current + (target - current) * k;
}

/** Perceptual shaping so quiet speech still visibly moves the orb but shouting stays controlled. */
export function shapeLevel(raw: number): number {
  const v = Math.max(0, Math.min(1, raw));
  return Math.pow(v, 0.6);
}

/** RMS of a byte time-domain buffer (128 = silence) -> 0..1, scaled for speech. */
export function rmsFromTimeDomain(bytes: Uint8Array): number {
  if (bytes.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < bytes.length; i++) {
    const d = (bytes[i] - 128) / 128;
    sum += d * d;
  }
  return Math.min(1, Math.sqrt(sum / bytes.length) * 3.2);
}
