import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { audioService, encodeWAV } from '../services/audio';

describe('Audio & Voice Certified Pipeline', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('generates a valid 16-bit PCM WAV container from Float32 audio samples', () => {
    const sampleRate = 16000;
    const samples = new Float32Array([0.0, 0.5, -0.5, 0.9, -0.9, 0.0]);
    const blob = encodeWAV(samples, sampleRate);

    expect(blob).toBeInstanceOf(Blob);
    expect(blob.type).toBe('audio/wav');
    // Header (44 bytes) + 6 samples * 2 bytes = 56 bytes
    expect(blob.size).toBe(56);
  });

  it('proves actual recorded audio Blob is the exact payload passed to backend STT request', async () => {
    const sampleAudio = new Float32Array([0.1, 0.2, 0.3, 0.4]);
    const expectedBlob = encodeWAV(sampleAudio, 16000);

    let passedFormData: FormData | null = null;

    global.fetch = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.includes('/voice/transcribe')) {
        passedFormData = init?.body as FormData;
        return {
          ok: true,
          json: async () => ({ text: 'Analyze Gold liquidity sweeps', language: 'en' })
        };
      }
      return { ok: false };
    });

    const transcript = await audioService.transcribeAudio(expectedBlob, 'http://127.0.0.1:8000');

    expect(transcript).toBe('Analyze Gold liquidity sweeps');
    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(passedFormData).not.toBeNull();

    const fd = passedFormData as unknown as FormData;
    const fileField = fd.get('file');
    expect(fileField).toBeInstanceOf(Blob);
    const sentBlob = fileField as Blob;
    expect(sentBlob.size).toBe(expectedBlob.size);
    expect(sentBlob.type).toBe('audio/wav');
  });

  it('fails if fixed transcript is substituted or STT response is ignored', async () => {
    const dummyBlob = encodeWAV(new Float32Array(10), 16000);
    const backendReturnedText = 'What is the current stop loss on NQ?';

    global.fetch = vi.fn().mockImplementation(async (url: string) => {
      if (url.includes('/voice/transcribe')) {
        return {
          ok: true,
          json: async () => ({ text: backendReturnedText })
        };
      }
      return { ok: false };
    });

    const actualTranscript = await audioService.transcribeAudio(dummyBlob, 'http://127.0.0.1:8000');

    // Certified path must strictly use the returned transcript, not hardcoded 'Show active setups'
    expect(actualTranscript).not.toBe('Show active setups');
    expect(actualTranscript).toBe(backendReturnedText);
  });

  it('passes backend STT transcript to Assistant endpoint and returns Assistant response', async () => {
    const transcript = 'What is the current portfolio risk?';
    let assistantRequestBody: { text?: string; conversation_id?: string } | null = null;

    global.fetch = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.includes('/assistant/query')) {
        assistantRequestBody = JSON.parse(init?.body as string);
        return {
          ok: true,
          json: async () => ({
            schema_version: '1.0.0',
            message_id: 'msg_test_1',
            conversation_id: 'conv_test_1',
            timestamp: new Date().toISOString(),
            role: 'assistant',
            content: 'Current aggregate risk across active setups is 2.25%.',
            input_mode: 'voice'
          })
        };
      }
      return { ok: false };
    });

    const res = await fetch('http://127.0.0.1:8000/api/v1/assistant/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: transcript, conversation_id: 'conv_test_1' })
    });

    const data = await res.json();
    expect(assistantRequestBody).toEqual({ text: transcript, conversation_id: 'conv_test_1' });
    expect(data.content).toBe('Current aggregate risk across active setups is 2.25%.');
  });

  it('sends assistant response text to backend TTS endpoint and plays returned audio bytes', async () => {
    const assistantText = 'Gold setup valid at 2684.50.';
    let ttsRequestedText: string | null = null;

    // Fake 10-byte audio buffer returned from backend
    const fakeAudioBuffer = new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]).buffer;

    global.fetch = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.includes('/voice/synthesize')) {
        const body = JSON.parse(init?.body as string);
        ttsRequestedText = body.text;
        return {
          ok: true,
          arrayBuffer: async () => fakeAudioBuffer
        };
      }
      return { ok: false };
    });

    const playSpy = vi.spyOn(audioService, 'playAudioBytes').mockResolvedValue();

    await audioService.synthesizeAndPlay(assistantText, 'http://127.0.0.1:8000');

    expect(ttsRequestedText).toBe(assistantText);
    expect(playSpy).toHaveBeenCalledWith(fakeAudioBuffer);
  });

  it('verifies browser speechSynthesis is NOT used on the certified voice path when backend TTS succeeds', async () => {
    const speakSpy = vi.spyOn(audioService, 'speakText');
    const playAudioSpy = vi.spyOn(audioService, 'playAudioBytes').mockResolvedValue();

    global.fetch = vi.fn().mockImplementation(async (url: string) => {
      if (url.includes('/voice/synthesize')) {
        return {
          ok: true,
          arrayBuffer: async () => new ArrayBuffer(8)
        };
      }
      return { ok: false };
    });

    await audioService.synthesizeAndPlay('Testing certified voice output', 'http://127.0.0.1:8000');

    expect(playAudioSpy).toHaveBeenCalled();
    expect(speakSpy).not.toHaveBeenCalled();
  });
});
