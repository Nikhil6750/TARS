/**
 * Local, energy-based Voice Activity Detection utterance segmenter.
 *
 * Captures raw microphone PCM via getUserMedia/Web Audio (no cloud speech
 * service involved — unlike SpeechRecognition, getUserMedia works reliably
 * inside a Tauri WebView2 webview) and emits one WAV blob per detected
 * utterance (speech onset -> trailing silence). Used by wake-word.ts for
 * both continuous "Hey TARS" listening and the one-shot post-wake command
 * capture, so audio never leaves the machine before a local transcription
 * call decides what was said.
 */
import { encodeWAV } from './audio';

export interface VadUtterance {
  blob: Blob;
  durationMs: number;
}

export interface VadEngineCallbacks {
  onUtterance: (utterance: VadUtterance) => void;
  onSpeechStart?: () => void;
  onLevel?: (rms: number) => void;
  onError?: (err: unknown) => void;
  /** Only used when `once` is set: fires if no utterance completes before `timeoutMs`. */
  onTimeout?: () => void;
}

export interface VadStartOptions {
  /** Stop the engine (and release the mic) after the first finalized utterance. */
  once?: boolean;
  /** Only meaningful with `once` — give up and fire onTimeout if nothing is heard in time. */
  timeoutMs?: number;
}

const FRAME_SIZE = 2048;
const CALIBRATION_FRAMES = 12;
const NOISE_FLOOR_MULTIPLIER = 2.5;
const MIN_THRESHOLD = 0.012;
const SILENCE_HANG_MS = 700;
const MIN_UTTERANCE_MS = 250;
const MAX_UTTERANCE_MS = 9000;
const PRE_ROLL_FRAMES = 4;

function rms(samples: Float32Array): number {
  let sum = 0;
  for (let i = 0; i < samples.length; i++) {
    sum += samples[i] * samples[i];
  }
  return Math.sqrt(sum / samples.length);
}

export class LocalVadEngine {
  private audioContext: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private sourceNode: MediaStreamAudioSourceNode | null = null;
  private processorNode: ScriptProcessorNode | null = null;
  private callbacks: VadEngineCallbacks | null = null;
  private running = false;
  private once = false;
  private timeoutHandle: ReturnType<typeof setTimeout> | null = null;

  private noiseFloor = MIN_THRESHOLD;
  private calibrationCount = 0;
  private calibrationSum = 0;

  private speechActive = false;
  private speechFrames: Float32Array[] = [];
  private preRoll: Float32Array[] = [];
  private silenceStreakMs = 0;
  private speechDurationMs = 0;
  private frameDurationMs = 0;

  public async start(callbacks: VadEngineCallbacks, options: VadStartOptions = {}): Promise<boolean> {
    if (this.running) return true;
    if (typeof navigator === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
      callbacks.onError?.(new Error('getUserMedia unavailable in this environment'));
      return false;
    }

    this.callbacks = callbacks;
    this.once = Boolean(options.once);
    this.resetUtteranceState();
    this.noiseFloor = MIN_THRESHOLD;
    this.calibrationCount = 0;
    this.calibrationSum = 0;

    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });

      const AudioContextClass =
        window.AudioContext ||
        (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      this.audioContext = new AudioContextClass();
      this.frameDurationMs = (FRAME_SIZE / this.audioContext.sampleRate) * 1000;

      this.sourceNode = this.audioContext.createMediaStreamSource(this.stream);
      this.processorNode = this.audioContext.createScriptProcessor(FRAME_SIZE, 1, 1);
      this.processorNode.onaudioprocess = (e) => this.handleFrame(e.inputBuffer.getChannelData(0));
      this.sourceNode.connect(this.processorNode);
      this.processorNode.connect(this.audioContext.destination);

      this.running = true;

      if (this.once && options.timeoutMs) {
        this.timeoutHandle = setTimeout(() => {
          if (this.running) {
            this.stop();
            this.callbacks?.onTimeout?.();
          }
        }, options.timeoutMs);
      }

      return true;
    } catch (err) {
      this.callbacks?.onError?.(err);
      this.stop();
      return false;
    }
  }

  public stop(): void {
    this.running = false;
    if (this.timeoutHandle !== null) {
      clearTimeout(this.timeoutHandle);
      this.timeoutHandle = null;
    }
    if (this.processorNode) {
      try {
        this.processorNode.disconnect();
      } catch {
        // ignore
      }
      this.processorNode = null;
    }
    if (this.sourceNode) {
      try {
        this.sourceNode.disconnect();
      } catch {
        // ignore
      }
      this.sourceNode = null;
    }
    if (this.stream) {
      this.stream.getTracks().forEach((t) => t.stop());
      this.stream = null;
    }
    if (this.audioContext && this.audioContext.state !== 'closed') {
      this.audioContext.close().catch(() => undefined);
    }
    this.audioContext = null;
    this.resetUtteranceState();
  }

  public isRunning(): boolean {
    return this.running;
  }

  private resetUtteranceState(): void {
    this.speechActive = false;
    this.speechFrames = [];
    this.preRoll = [];
    this.silenceStreakMs = 0;
    this.speechDurationMs = 0;
  }

  private handleFrame(input: Float32Array): void {
    if (!this.running) return;
    const level = rms(input);
    this.callbacks?.onLevel?.(Math.min(1, level * 8));

    // Calibrate ambient noise floor from the first few frames (assumed silence).
    if (this.calibrationCount < CALIBRATION_FRAMES) {
      this.calibrationCount++;
      this.calibrationSum += level;
      this.noiseFloor = Math.max(MIN_THRESHOLD, (this.calibrationSum / this.calibrationCount) * NOISE_FLOOR_MULTIPLIER);
      return;
    }

    const threshold = Math.max(MIN_THRESHOLD, this.noiseFloor);
    const isSpeechFrame = level >= threshold;
    const frame = new Float32Array(input);

    if (!this.speechActive) {
      // Keep a short rolling pre-roll buffer so utterance onset isn't clipped.
      this.preRoll.push(frame);
      if (this.preRoll.length > PRE_ROLL_FRAMES) {
        this.preRoll.shift();
      }
      if (isSpeechFrame) {
        this.speechActive = true;
        this.callbacks?.onSpeechStart?.();
        this.speechFrames = [...this.preRoll, frame];
        this.speechDurationMs = this.speechFrames.length * this.frameDurationMs;
        this.silenceStreakMs = 0;
      }
      return;
    }

    // Already inside an utterance.
    this.speechFrames.push(frame);
    this.speechDurationMs += this.frameDurationMs;

    if (isSpeechFrame) {
      this.silenceStreakMs = 0;
    } else {
      this.silenceStreakMs += this.frameDurationMs;
    }

    const trailingSilenceLongEnough = this.silenceStreakMs >= SILENCE_HANG_MS;
    const utteranceTooLong = this.speechDurationMs >= MAX_UTTERANCE_MS;

    if (trailingSilenceLongEnough || utteranceTooLong) {
      this.finalizeUtterance();
    }
  }

  private finalizeUtterance(): void {
    const durationMs = this.speechDurationMs;
    const frames = this.speechFrames;
    this.resetUtteranceState();

    if (durationMs < MIN_UTTERANCE_MS || frames.length === 0 || !this.audioContext) {
      // Too short to be real speech (a click/breath) — discard and keep listening.
      return;
    }

    let totalSamples = 0;
    for (const f of frames) totalSamples += f.length;
    const merged = new Float32Array(totalSamples);
    let offset = 0;
    for (const f of frames) {
      merged.set(f, offset);
      offset += f.length;
    }

    const blob = encodeWAV(merged, this.audioContext.sampleRate);
    this.callbacks?.onUtterance({ blob, durationMs });

    if (this.once) {
      this.stop();
    }
  }
}
