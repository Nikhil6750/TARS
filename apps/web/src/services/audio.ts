/**
 * Audio and Push-to-Talk Service
 * Handles microphone capture, visualizer frequency/volume data, and TTS audio playback.
 */

export interface AudioVisualizerCallback {
  (volume: number, frequencyData: Uint8Array): void;
}

class AudioService {
  private audioContext: AudioContext | null = null;
  private mediaStream: MediaStream | null = null;
  private analyser: AnalyserNode | null = null;
  private animationFrameId: number | null = null;
  private isRecording = false;

  public async requestMicrophonePermission(): Promise<boolean> {
    if (typeof navigator === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
      return false;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      // Release tracks immediately after permission check
      stream.getTracks().forEach((track) => track.stop());
      return true;
    } catch (err) {
      console.warn('Microphone permission denied or unavailable:', err);
      return false;
    }
  }

  public async startPushToTalk(onAudioData?: AudioVisualizerCallback): Promise<boolean> {
    if (this.isRecording) return true;

    try {
      this.mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });

      const AudioContextClass = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      this.audioContext = new AudioContextClass();
      const sourceNode = this.audioContext.createMediaStreamSource(this.mediaStream);
      this.analyser = this.audioContext.createAnalyser();
      this.analyser.fftSize = 64;
      this.analyser.smoothingTimeConstant = 0.8;
      sourceNode.connect(this.analyser);

      this.isRecording = true;

      if (onAudioData) {
        const bufferLength = this.analyser.frequencyBinCount;
        const dataArray = new Uint8Array(bufferLength);

        const updateLoop = () => {
          if (!this.isRecording || !this.analyser) return;

          this.analyser.getByteFrequencyData(dataArray);

          // Calculate average volume from 0.0 to 1.0
          let sum = 0;
          for (let i = 0; i < bufferLength; i++) {
            sum += dataArray[i];
          }
          const volume = Math.min(1, (sum / bufferLength) / 128);

          onAudioData(volume, dataArray);
          this.animationFrameId = requestAnimationFrame(updateLoop);
        };

        updateLoop();
      }

      return true;
    } catch (err) {
      console.error('Failed to start Push-to-Talk audio recording:', err);
      this.stopPushToTalk();
      return false;
    }
  }

  public stopPushToTalk(): void {
    this.isRecording = false;

    if (this.animationFrameId !== null) {
      cancelAnimationFrame(this.animationFrameId);
      this.animationFrameId = null;
    }

    if (this.mediaStream) {
      this.mediaStream.getTracks().forEach((t) => t.stop());
      this.mediaStream = null;
    }

    if (this.audioContext && this.audioContext.state !== 'closed') {
      this.audioContext.close().catch(() => {});
      this.audioContext = null;
    }

    this.analyser = null;
  }

  public speakText(text: string, rate = 1.0, volume = 0.9): Promise<void> {
    return new Promise((resolve) => {
      if (typeof window === 'undefined' || !('speechSynthesis' in window)) {
        resolve();
        return;
      }

      window.speechSynthesis.cancel();

      const utterance = new SpeechSynthesisUtterance(text);
      utterance.rate = rate;
      utterance.volume = volume;
      utterance.pitch = 0.95; // Slightly robotic/deep TARS voice profile

      // Select natural or robotic sounding voice if available
      const voices = window.speechSynthesis.getVoices();
      const preferred = voices.find((v) =>
        v.name.includes('Google') || v.name.includes('Natural') || v.name.includes('David') || v.lang.startsWith('en')
      );
      if (preferred) {
        utterance.voice = preferred;
      }

      utterance.onend = () => resolve();
      utterance.onerror = () => resolve();

      window.speechSynthesis.speak(utterance);
    });
  }

  public stopSpeaking(): void {
    if (typeof window !== 'undefined' && 'speechSynthesis' in window) {
      window.speechSynthesis.cancel();
    }
  }
}

export const audioService = new AudioService();
