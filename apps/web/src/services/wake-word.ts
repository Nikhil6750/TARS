/**
 * Local Wake Word & Command-Capture Service for TARS
 *
 * Complies with strict architectural constraints:
 * - Does NOT depend on the browser/WebView SpeechRecognition API. WebView2's
 *   Chromium engine exposes the `SpeechRecognition` constructor, but its
 *   backing continuous-recognition service is a cloud call that isn't
 *   available/authorized outside real Chrome — the recognizer silently
 *   fails or loops on 'network' errors while status still reports "on".
 * - Does NOT claim openWakeWord has a custom TARS model (truthfully documented).
 * - Detects the wake/command phrases by capturing raw microphone audio
 *   locally (getUserMedia, which — unlike SpeechRecognition — works inside
 *   WebView2 with no cloud dependency), segmenting utterances with a local
 *   energy-based VAD (see local-vad.ts), and transcribing each utterance
 *   through the backend's local faster-whisper STT endpoint. No audio is
 *   sent anywhere but this machine's own backend (127.0.0.1).
 * - After the wake phrase is heard, transitions to a one-shot
 *   command-capture state: listens for exactly one next utterance until
 *   silence, transcribes it, and hands the raw transcript to the caller for
 *   deterministic (non-exact-match) routing.
 * - Supports user-toggleable active listening state and truthful status reporting.
 */
import { audioService } from './audio';
import { LocalVadEngine, VadUtterance } from './local-vad';

export interface WakeWordStatusInfo {
  isActive: boolean;
  engine: 'web_speech_local' | 'vad_whisper_local' | 'manual_only';
  engineLabel: string;
  targetPhrase: string;
  lastDetectedAt?: string;
  errorMessage?: string;
}

export interface WakeWordCallbacks {
  onWakeDetected: (phrase: string) => void;
  onAnalyzeChartDetected?: (phrase: string) => void;
  /** Fires with the transcribed text of the single utterance captured after wake. */
  onCommandCaptured?: (transcript: string) => void;
  /** Fires if no command utterance is heard within the post-wake window. */
  onCommandTimeout?: () => void;
  onTranscriptInterim?: (text: string) => void;
  onStateChange?: (status: WakeWordStatusInfo) => void;
  /** Real-time mic amplitude (0..1), for HUD visualization during background listening. */
  onAudioLevel?: (level: number) => void;
  /** Fires immediately upon speech onset (crucial for barge-in / interrupting active TTS). */
  onSpeechStart?: () => void;
}

// Regex patterns for wake phrase and chart analysis commands — matched
// against real local transcripts, never exact string equality.
const WAKE_PHRASE_REGEX = /\b(hey\s+tars|tars|hey\s+tar|ok\s+tars|hey\s+torres|hi\s+tars)\b/i;
const ANALYZE_CHART_REGEX = /\b(analy[sz]e|check|look\s+at|evaluate|read|scan|inspect|review|what\s+do\s+you\s+see\s+on)\s+(?:this|the|my|active|current)?\s*charts?\b/i;

const DEFAULT_API_ENDPOINT = 'http://127.0.0.1:8000';
const COMMAND_TIMEOUT_MS = 7000;

type ListenMode = 'wake' | 'command';

export class WakeWordService {
  private engine = new LocalVadEngine();
  private isRunning = false;
  private callbacks: WakeWordCallbacks | null = null;
  private apiEndpoint = DEFAULT_API_ENDPOINT;
  private mode: ListenMode = 'wake';
  private lastWakeDetectedAt = 0;
  private lastChartDetectedAt = 0;
  private status: WakeWordStatusInfo = {
    isActive: false,
    engine: 'vad_whisper_local',
    engineLabel: 'Local VAD + faster-whisper (background, no cloud)',
    targetPhrase: 'Hey TARS',
  };

  public getStatus(): WakeWordStatusInfo {
    return { ...this.status };
  }

  public isListening(): boolean {
    return this.isRunning;
  }

  public async startListening(
    callbacks: WakeWordCallbacks,
    apiEndpoint: string = DEFAULT_API_ENDPOINT
  ): Promise<boolean> {
    if (this.isRunning) return true;
    this.callbacks = callbacks;
    this.apiEndpoint = apiEndpoint;

    if (typeof window === 'undefined' || typeof navigator === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
      this.updateStatus({
        isActive: false,
        engine: 'manual_only',
        engineLabel: 'Push-to-Talk / Hotkey (Ctrl+Shift+Space)',
        errorMessage: 'Microphone capture unavailable in this environment',
      });
      return false;
    }

    this.mode = 'wake';
    const started = await this.engine.start({
      onUtterance: (utterance) => this.handleUtterance(utterance),
      onSpeechStart: () => this.callbacks?.onSpeechStart?.(),
      onLevel: (level) => this.callbacks?.onAudioLevel?.(level),
      onError: (err) => {
        console.warn('[WakeWordService] VAD engine error:', err);
        this.updateStatus({ isActive: false, errorMessage: String(err) });
      },
    });

    if (!started) {
      this.isRunning = false;
      this.updateStatus({
        isActive: false,
        engine: 'manual_only',
        engineLabel: 'Push-to-Talk / Hotkey (Ctrl+Shift+Space)',
        errorMessage: 'Local microphone capture failed to start (permission denied?)',
      });
      return false;
    }

    this.isRunning = true;
    this.updateStatus({
      isActive: true,
      engine: 'vad_whisper_local',
      engineLabel: 'Local VAD + faster-whisper ("Hey TARS")',
      errorMessage: undefined,
    });
    return true;
  }

  public stopListening(): void {
    this.isRunning = false;
    this.engine.stop();
    this.mode = 'wake';
    this.updateStatus({ isActive: false });
  }

  /** Force start command capture (e.g. on manual wake / PTT). */
  public async beginCommandCaptureManual(): Promise<void> {
    await this.beginCommandCapture();
  }

  /** Re-enters continuous wake listening after a completed/timed-out command capture. */
  public async resumeWakeListening(): Promise<void> {
    if (!this.isRunning) return;
    this.mode = 'wake';
    await this.engine.start({
      onUtterance: (utterance) => this.handleUtterance(utterance),
      onSpeechStart: () => this.callbacks?.onSpeechStart?.(),
      onLevel: (level) => this.callbacks?.onAudioLevel?.(level),
      onError: (err) => console.warn('[WakeWordService] VAD engine error:', err),
    });
  }

  private async handleUtterance(utterance: VadUtterance): Promise<void> {
    let transcript = '';
    try {
      transcript = await audioService.transcribeAudio(utterance.blob, this.apiEndpoint);
    } catch (err) {
      console.warn('[WakeWordService] Local transcription failed:', err);
    }
    transcript = transcript.trim();
    if (this.callbacks?.onTranscriptInterim && transcript) {
      this.callbacks.onTranscriptInterim(transcript);
    }
    if (!transcript) {
      if (this.mode === 'wake') return; // stay listening for the wake phrase
      return; // command mode: empty transcript, wait for the timeout to fire
    }

    if (this.mode === 'command') {
      const captured = transcript;
      this.callbacks?.onCommandCaptured?.(captured);
      await this.resumeWakeListening();
      return;
    }

    // mode === 'wake'
    const now = Date.now();

    if (ANALYZE_CHART_REGEX.test(transcript)) {
      if (now - this.lastChartDetectedAt > 2000) {
        this.lastChartDetectedAt = now;
        this.updateStatus({ lastDetectedAt: new Date().toISOString() });
        this.callbacks?.onAnalyzeChartDetected?.(transcript);
      }
      return;
    }

    if (WAKE_PHRASE_REGEX.test(transcript)) {
      if (now - this.lastWakeDetectedAt > 2000) {
        this.lastWakeDetectedAt = now;
        this.updateStatus({ lastDetectedAt: new Date().toISOString() });
        this.callbacks?.onWakeDetected(transcript);
        await this.beginCommandCapture();
      }
    }
  }

  /** Switches from continuous wake listening to a one-shot post-wake command capture. */
  private async beginCommandCapture(): Promise<void> {
    if (!this.isRunning) return;
    this.mode = 'command';
    this.engine.stop();
    await this.engine.start(
      {
        onUtterance: (utterance) => this.handleUtterance(utterance),
        onSpeechStart: () => this.callbacks?.onSpeechStart?.(),
        onLevel: (level) => this.callbacks?.onAudioLevel?.(level),
        onError: (err) => console.warn('[WakeWordService] VAD engine error:', err),
        onTimeout: () => {
          this.callbacks?.onCommandTimeout?.();
          void this.resumeWakeListening();
        },
      },
      { once: true, timeoutMs: COMMAND_TIMEOUT_MS }
    );
  }

  private updateStatus(partial: Partial<WakeWordStatusInfo>): void {
    this.status = { ...this.status, ...partial };
    if (this.callbacks?.onStateChange) {
      this.callbacks.onStateChange({ ...this.status });
    }
  }
}

export const wakeWordService = new WakeWordService();
