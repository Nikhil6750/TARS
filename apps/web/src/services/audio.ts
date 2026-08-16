/**
 * Audio and Push-to-Talk Service for TARS V1
 * Certified voice path:
 * Microphone -> Real Audio Blob -> POST /api/v1/voice/transcribe -> Backend STT Transcript
 * -> POST /api/v1/assistant/query -> Backend Assistant Response
 * -> POST /api/v1/voice/synthesize -> Backend TTS WAV audio -> Native/Web Audio Playback.
 */

export interface AudioVisualizerCallback {
  (volume: number, frequencyData: Uint8Array): void;
}

export function writeWavString(view: DataView, offset: number, str: string): void {
  for (let i = 0; i < str.length; i++) {
    view.setUint8(offset + i, str.charCodeAt(i));
  }
}

export function encodeWAV(samples: Float32Array, sampleRate = 16000): Blob {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);

  // RIFF chunk descriptor
  writeWavString(view, 0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  writeWavString(view, 8, 'WAVE');

  // fmt sub-chunk
  writeWavString(view, 12, 'fmt ');
  view.setUint32(16, 16, true); // SubChunk1Size (16 for PCM)
  view.setUint16(20, 1, true); // AudioFormat (1 = PCM)
  view.setUint16(22, 1, true); // NumChannels (1 = Mono)
  view.setUint32(24, sampleRate, true); // SampleRate
  view.setUint32(28, sampleRate * 2, true); // ByteRate (SampleRate * NumChannels * BitsPerSample/8)
  view.setUint16(32, 2, true); // BlockAlign (NumChannels * BitsPerSample/8)
  view.setUint16(34, 16, true); // BitsPerSample (16 bits)

  // data sub-chunk
  writeWavString(view, 36, 'data');
  view.setUint32(40, samples.length * 2, true);

  // Write PCM samples
  let offset = 44;
  for (let i = 0; i < samples.length; i++, offset += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }

  return new Blob([view], { type: 'audio/wav' });
}

export class AudioService {
  private audioContext: AudioContext | null = null;
  private mediaStream: MediaStream | null = null;
  private analyser: AnalyserNode | null = null;
  private processorNode: ScriptProcessorNode | null = null;
  private animationFrameId: number | null = null;
  private isRecording = false;
  private audioChunks: Float32Array[] = [];
  private sampleRate = 16000;
  private activeAudioElement: HTMLAudioElement | null = null;
  private mediaRecorder: MediaRecorder | null = null;
  private recordedBlobs: Blob[] = [];

  public async requestMicrophonePermission(): Promise<boolean> {
    if (typeof navigator === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
      return false;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
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
      this.audioChunks = [];
      this.recordedBlobs = [];
      this.mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });

      if (typeof MediaRecorder !== 'undefined') {
        try {
          this.mediaRecorder = new MediaRecorder(this.mediaStream);
          this.mediaRecorder.ondataavailable = (evt: BlobEvent) => {
            if (evt.data && evt.data.size > 0) {
              this.recordedBlobs.push(evt.data);
            }
          };
          this.mediaRecorder.start();
        } catch (err) {
          console.warn('[TARS Audio] MediaRecorder init error:', err);
          this.mediaRecorder = null;
        }
      }

      const AudioContextClass =
        window.AudioContext ||
        (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      this.audioContext = new AudioContextClass();
      this.sampleRate = this.audioContext.sampleRate || 16000;

      const sourceNode = this.audioContext.createMediaStreamSource(this.mediaStream);
      this.analyser = this.audioContext.createAnalyser();
      this.analyser.fftSize = 64;
      this.analyser.smoothingTimeConstant = 0.8;
      sourceNode.connect(this.analyser);

      // ScriptProcessor to capture raw PCM float chunks
      if (this.audioContext.createScriptProcessor) {
        this.processorNode = this.audioContext.createScriptProcessor(4096, 1, 1);
        this.processorNode.onaudioprocess = (e) => {
          if (!this.isRecording) return;
          const input = e.inputBuffer.getChannelData(0);
          this.audioChunks.push(new Float32Array(input));
        };
        sourceNode.connect(this.processorNode);
        this.processorNode.connect(this.audioContext.destination);
      }

      this.isRecording = true;

      if (onAudioData && this.analyser) {
        const bufferLength = this.analyser.frequencyBinCount;
        const dataArray = new Uint8Array(bufferLength);

        const updateLoop = () => {
          if (!this.isRecording || !this.analyser) return;

          this.analyser.getByteFrequencyData(dataArray);

          let sum = 0;
          for (let i = 0; i < bufferLength; i++) {
            sum += dataArray[i];
          }
          const volume = Math.min(1, sum / bufferLength / 128);

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

  public async stopPushToTalk(): Promise<Blob | null> {
    this.isRecording = false;

    if (this.animationFrameId !== null) {
      cancelAnimationFrame(this.animationFrameId);
      this.animationFrameId = null;
    }

    if (this.processorNode) {
      try {
        this.processorNode.disconnect();
      } catch {
        // ignore
      }
      this.processorNode = null;
    }

    if (this.mediaRecorder && this.mediaRecorder.state !== 'inactive') {
      try {
        const recorder = this.mediaRecorder;
        await new Promise<void>((resolve) => {
          if (recorder.state === 'inactive') {
            resolve();
            return;
          }
          const prevStop = recorder.onstop;
          recorder.onstop = (ev: Event) => {
            if (typeof prevStop === 'function') prevStop.call(recorder, ev);
            resolve();
          };
          recorder.stop();
        });
      } catch (err) {
        console.warn('[TARS Audio] MediaRecorder stop error:', err);
      }
    }

    if (this.mediaStream) {
      this.mediaStream.getTracks().forEach((t) => t.stop());
      this.mediaStream = null;
    }

    // Merge recorded PCM chunks into single Float32Array
    let totalSamples = 0;
    for (const chunk of this.audioChunks) {
      totalSamples += chunk.length;
    }

    let recordedBlob: Blob | null = null;
    if (this.recordedBlobs.length > 0) {
      const mime = (this.mediaRecorder && this.mediaRecorder.mimeType) || 'audio/wav';
      recordedBlob = new Blob(this.recordedBlobs, { type: mime });
    } else if (totalSamples > 0) {
      const mergedSamples = new Float32Array(totalSamples);
      let offset = 0;
      for (const chunk of this.audioChunks) {
        mergedSamples.set(chunk, offset);
        offset += chunk.length;
      }
      recordedBlob = encodeWAV(mergedSamples, this.sampleRate);
    } else {
      // Fallback 1-second silence buffer if empty recording
      recordedBlob = encodeWAV(new Float32Array(1600), 16000);
    }

    this.audioChunks = [];
    this.recordedBlobs = [];
    this.mediaRecorder = null;

    if (this.audioContext && this.audioContext.state !== 'closed') {
      try {
        await this.audioContext.close();
      } catch {
        // ignore
      }
      this.audioContext = null;
    }

    this.analyser = null;
    return recordedBlob;
  }

  /**
   * Transcribe recorded audio bytes via backend STT endpoint
   * @param audioBlob The exact audio/wav Blob captured from the microphone
   * @param apiEndpoint Backend HTTP URL (e.g. http://127.0.0.1:8000)
   */
  public async transcribeAudio(audioBlob: Blob, apiEndpoint: string): Promise<string> {
    if (!audioBlob || audioBlob.size === 0) {
      throw new Error('No audio data captured to transcribe');
    }

    const formData = new FormData();
    formData.append('file', audioBlob, 'recording.wav');

    const res = await fetch(`${apiEndpoint}/api/v1/voice/transcribe`, {
      method: 'POST',
      body: formData,
    });

    if (!res.ok) {
      // Try fallback route /api/voice/transcribe
      const fallbackRes = await fetch(`${apiEndpoint}/api/voice/transcribe`, {
        method: 'POST',
        body: formData,
      });
      if (!fallbackRes.ok) {
        throw new Error(`Transcription failed with status ${res.status}`);
      }
      const data = await fallbackRes.json();
      return (data && data.text) ? String(data.text).trim() : '';
    }

    const data = await res.json();
    return (data && data.text) ? String(data.text).trim() : '';
  }

  /**
   * Synthesize text to speech using backend TTS endpoint and play returned audio bytes
   * @param text Text to synthesize
   * @param apiEndpoint Backend HTTP URL
   */
  public async synthesizeAndPlay(text: string, apiEndpoint: string): Promise<void> {
    if (!text || !text.trim()) return;

    let res = await fetch(`${apiEndpoint}/api/v1/voice/synthesize`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });

    if (!res.ok) {
      res = await fetch(`${apiEndpoint}/api/voice/synthesize`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
    }

    if (!res.ok) {
      throw new Error(`Backend speech synthesis failed with status ${res.status}`);
    }

    const audioBytes = await res.arrayBuffer();
    await this.playAudioBytes(audioBytes);
  }

  /**
   * Play raw audio bytes received from backend TTS
   */
  public playAudioBytes(audioBytes: ArrayBuffer): Promise<void> {
    return new Promise((resolve, reject) => {
      try {
        const blob = new Blob([audioBytes], { type: 'audio/wav' });
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        this.activeAudioElement = audio;

        audio.onended = () => {
          URL.revokeObjectURL(url);
          this.activeAudioElement = null;
          resolve();
        };

        audio.onerror = (err) => {
          URL.revokeObjectURL(url);
          this.activeAudioElement = null;
          reject(err);
        };

        audio.play().catch(reject);
      } catch (err) {
        reject(err);
      }
    });
  }

  /**
   * Non-certified browser speech synthesis fallback (dev / offline only)
   */
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
      utterance.pitch = 0.95;

      const voices = window.speechSynthesis.getVoices();
      const preferred = voices.find(
        (v) =>
          v.name.includes('Google') ||
          v.name.includes('Natural') ||
          v.name.includes('David') ||
          v.lang.startsWith('en')
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
    if (this.activeAudioElement) {
      this.activeAudioElement.pause();
      this.activeAudioElement = null;
    }
    if (typeof window !== 'undefined' && 'speechSynthesis' in window) {
      window.speechSynthesis.cancel();
    }
  }
}

export const audioService = new AudioService();
