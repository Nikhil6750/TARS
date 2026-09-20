import { rmsFromTimeDomain } from '../orb/audioLevels';

/**
 * Gapless streaming playback of PCM16 mono chunks (Gemini Live audio, 24 kHz).
 * Chunks are scheduled back to back on one AudioContext clock; stop() cuts everything
 * that is playing or queued immediately (barge-in).
 */
export class PcmStreamPlayer {
  private ctx: AudioContext | null = null;
  private nextTime = 0;
  private sources = new Set<AudioBufferSourceNode>();
  private started = false;
  private analyser: AnalyserNode | null = null;
  private buf: Uint8Array<ArrayBuffer> | null = null;

  constructor(private onStarted?: () => void) {}

  private context(): AudioContext {
    if (!this.ctx) {
      this.ctx = new AudioContext({ sampleRate: 24000 });
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 512;
      this.analyser.connect(this.ctx.destination);
      this.buf = new Uint8Array(new ArrayBuffer(this.analyser.fftSize));
    }
    return this.ctx;
  }

  play(base64: string, sampleRate = 24000): void {
    const ctx = this.context();
    if (ctx.state === 'suspended') void ctx.resume();
    const bytes = Uint8Array.from(atob(base64), c => c.charCodeAt(0));
    const samples = new Int16Array(bytes.buffer, bytes.byteOffset, Math.floor(bytes.byteLength / 2));
    if (samples.length === 0) return;
    const buffer = ctx.createBuffer(1, samples.length, sampleRate);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i++) channel[i] = samples[i] / 32768;
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(this.analyser ?? ctx.destination);
    const startAt = Math.max(ctx.currentTime + 0.03, this.nextTime);
    source.start(startAt);
    this.nextTime = startAt + buffer.duration;
    this.sources.add(source);
    source.onended = () => this.sources.delete(source);
    if (!this.started) {
      this.started = true;
      this.onStarted?.();
    }
  }

  stop(): void {
    for (const source of this.sources) {
      try { source.stop(); } catch { /* already ended */ }
    }
    this.sources.clear();
    this.nextTime = 0;
    this.started = false;
  }

  /** Real amplitude of what is playing right now (0..1); 0 when silent. Read from an AnalyserNode. */
  level(): number {
    if (!this.analyser || !this.buf || this.sources.size === 0) return 0;
    this.analyser.getByteTimeDomainData(this.buf);
    return rmsFromTimeDomain(this.buf);
  }

  get playing(): boolean {
    return this.sources.size > 0;
  }
}
