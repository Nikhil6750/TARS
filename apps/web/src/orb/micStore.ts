/** Latest native microphone facts (device, levels, cadence) as reported by the capture thread. */
export interface MicInfo {
  status: 'OPEN' | 'STARTING' | 'NO_DEVICE' | 'ERROR';
  device: string;
  selected_by: string;
  sample_rate: number;
  channels: number;
  format: string;
  frames_total: number;
  frames_per_sec: number;
  rms_db: number;
  max_db: number;
  silent_for_s: number;
  open_for_s: number;
  error: string | null;
}

/** Digital-silence threshold; keep equal to SILENT_DB in wake_engine.rs. */
export const SILENT_DB = -85;

type Listener = () => void;

class MicStore {
  private info: MicInfo | null = null;
  private listeners = new Set<Listener>();
  subscribe = (fn: Listener) => {
    this.listeners.add(fn);
    return () => { this.listeners.delete(fn); };
  };
  getSnapshot = () => this.info;
  set(info: MicInfo) {
    this.info = info;
    this.listeners.forEach(fn => fn());
  }
}

export const micStore = new MicStore();

/** Meter position 0..1 for a dBFS level (-70 dB floor, -10 dB ceiling). */
export function meterFraction(db: number): number {
  return Math.max(0, Math.min(1, (db + 70) / 60));
}

export interface MicTestResult {
  verdict: 'PASS' | 'MICROPHONE SILENT' | 'NO SIGNAL';
  peakDb: number;
  device: string;
}

/** PASS only when meaningful energy (above the silence floor) was actually received. */
export function judgeMicTest(peakDb: number, framesSeen: number, device: string): MicTestResult {
  if (framesSeen === 0) return { verdict: 'NO SIGNAL', peakDb, device };
  return { verdict: peakDb > -55 ? 'PASS' : 'MICROPHONE SILENT', peakDb, device };
}
