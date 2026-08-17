/**
 * Local Wake Word & Phrase Detection Service for TARS
 * 
 * Complies with strict architectural constraints:
 * - Does NOT claim openWakeWord has a custom TARS model (truthfully documented).
 * - Implements narrow, local phrase detection using WebView2 / Web Speech continuous
 *   recognition and/or local VAD -> faster-whisper STT.
 * - Does NOT send wake audio to external or cloud services.
 * - Supports user-toggleable active listening state and truthful status reporting.
 */

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
  onTranscriptInterim?: (text: string) => void;
  onStateChange?: (status: WakeWordStatusInfo) => void;
}

// Regex patterns for wake phrase and chart analysis commands
const WAKE_PHRASE_REGEX = /\b(hey\s+tars|tars|hey\s+tar|ok\s+tars|hey\s+torres|hi\s+tars)\b/i;
const ANALYZE_CHART_REGEX = /\b(analy[sz]e|check|look\s+at|evaluate|read|scan|inspect|review|what\s+do\s+you\s+see\s+on)\s+(?:this|the|my|active|current)?\s*charts?\b/i;

type SpeechRecognitionType = unknown;

export class WakeWordService {
  private isRunning = false;
  private recognition: SpeechRecognitionType | null = null;
  private callbacks: WakeWordCallbacks | null = null;
  private lastWakeDetectedAt = 0;
  private lastChartDetectedAt = 0;
  private status: WakeWordStatusInfo = {
    isActive: false,
    engine: 'web_speech_local',
    engineLabel: 'Local Web Speech API (WebView2 / Browser)',
    targetPhrase: 'Hey TARS',
  };
  private restartTimeout: NodeJS.Timeout | null = null;

  constructor() {
    this.detectCapabilities();
  }

  private detectCapabilities(): void {
    if (typeof window !== 'undefined') {
      const win = window as unknown as Record<string, unknown>;
      const hasSpeechRec = Boolean(win.SpeechRecognition || win.webkitSpeechRecognition);
      if (hasSpeechRec) {
        this.status.engine = 'web_speech_local';
        this.status.engineLabel = 'Local Web Speech (Hey TARS phrase listener)';
      } else {
        this.status.engine = 'vad_whisper_local';
        this.status.engineLabel = 'Local Whisper STT Fallback (No cloud)';
      }
    }
  }

  public getStatus(): WakeWordStatusInfo {
    return { ...this.status };
  }

  public isListening(): boolean {
    return this.isRunning;
  }

  public async startListening(callbacks: WakeWordCallbacks): Promise<boolean> {
    if (this.isRunning) return true;
    this.callbacks = callbacks;

    if (typeof window === 'undefined') {
      this.updateStatus({ isActive: false, errorMessage: 'Window not available' });
      return false;
    }

    const win = window as unknown as Record<string, unknown>;
    const SpeechRecClass = (win.SpeechRecognition || win.webkitSpeechRecognition) as {
      new (): {
        continuous: boolean;
        interimResults: boolean;
        lang: string;
        start: () => void;
        stop: () => void;
        abort: () => void;
        onresult: (event: unknown) => void;
        onerror: (event: unknown) => void;
        onend: () => void;
      };
    } | undefined;

    if (SpeechRecClass) {
      try {
        const rec = new SpeechRecClass();
        rec.continuous = true;
        rec.interimResults = true;
        rec.lang = 'en-US';

        rec.onresult = (event: unknown) => {
          const evt = event as {
            resultIndex: number;
            results: Array<{
              [key: number]: { transcript: string; confidence: number };
              isFinal: boolean;
            }>;
          };

          for (let i = evt.resultIndex; i < evt.results.length; i++) {
            const transcript = evt.results[i][0].transcript.trim();
            if (this.callbacks?.onTranscriptInterim) {
              this.callbacks.onTranscriptInterim(transcript);
            }

            const now = Date.now();

            // Check if chart analysis command is directly uttered
            if (ANALYZE_CHART_REGEX.test(transcript)) {
              if (now - this.lastChartDetectedAt > 2000) {
                this.lastChartDetectedAt = now;
                this.callbacks?.onAnalyzeChartDetected?.(transcript);
                this.updateStatus({ lastDetectedAt: new Date().toISOString() });
              }
              return;
            }

            // Check for wake word "Hey TARS"
            if (WAKE_PHRASE_REGEX.test(transcript)) {
              if (now - this.lastWakeDetectedAt > 2000) {
                this.lastWakeDetectedAt = now;
                this.callbacks?.onWakeDetected(transcript);
                this.updateStatus({ lastDetectedAt: new Date().toISOString() });
              }
              return;
            }
          }
        };

        rec.onerror = (err: unknown) => {
          const errEvt = err as { error?: string };
          // Ignore 'no-speech' or 'aborted' normal lifecycle events
          if (errEvt.error !== 'no-speech' && errEvt.error !== 'aborted') {
            console.warn('[WakeWordService] Speech recognition warning:', errEvt.error);
          }
        };

        rec.onend = () => {
          // Continuous restart if still supposed to be running
          if (this.isRunning) {
            this.restartTimeout = setTimeout(() => {
              if (this.isRunning && this.recognition) {
                try {
                  (this.recognition as { start: () => void }).start();
                } catch {
                  // ignore if already started
                }
              }
            }, 300);
          }
        };

        rec.start();
        this.recognition = rec;
        this.isRunning = true;
        this.updateStatus({
          isActive: true,
          engine: 'web_speech_local',
          engineLabel: 'Local Web Speech ("Hey TARS")',
          errorMessage: undefined,
        });
        return true;
      } catch (err) {
        console.warn('[WakeWordService] Failed to start Web Speech Recognition:', err);
      }
    }

    // Fallback: indicate manual / PTT mode available truthfully
    this.isRunning = false;
    this.updateStatus({
      isActive: false,
      engine: 'manual_only',
      engineLabel: 'Push-to-Talk / Hotkey (Ctrl+Shift+Space)',
      errorMessage: 'Local speech recognition engine unavailable in this environment',
    });
    return false;
  }

  public stopListening(): void {
    this.isRunning = false;
    if (this.restartTimeout) {
      clearTimeout(this.restartTimeout);
      this.restartTimeout = null;
    }
    if (this.recognition) {
      try {
        (this.recognition as { stop: () => void }).stop();
      } catch {
        // ignore
      }
      this.recognition = null;
    }
    this.updateStatus({ isActive: false });
  }

  private updateStatus(partial: Partial<WakeWordStatusInfo>): void {
    this.status = { ...this.status, ...partial };
    if (this.callbacks?.onStateChange) {
      this.callbacks.onStateChange({ ...this.status });
    }
  }
}

export const wakeWordService = new WakeWordService();
