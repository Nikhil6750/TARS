import { afterEach, describe, expect, it, vi } from 'vitest';
import { audioService, encodeWAV } from '../services/audio';

describe('canonical audio transport', () => {
  afterEach(() => vi.restoreAllMocks());

  it('generates a valid 16-bit PCM WAV container', () => {
    const blob = encodeWAV(new Float32Array([0, 0.5, -0.5, 0.9, -0.9, 0]), 16000);
    expect(blob.type).toBe('audio/wav');
    expect(blob.size).toBe(56);
  });

  it('submits one complete WAV directly to the canonical utterance endpoint', async () => {
    const wav = encodeWAV(new Float32Array([0.1, 0.2, 0.3]), 16000);
    const submitted: { current: FormData | null } = { current: null };
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) => {
      submitted.current = init?.body as FormData;
      return new Response(
        JSON.stringify({
          turn_id: 'turn-1',
          display_text: 'Yeah?',
          speech_text: 'Yeah?',
          intent: 'DETERMINISTIC',
          status: 'awaiting_command',
          provider: 'wake_matcher',
          latency_ms: 123,
          conversation_id: 'conv-1',
          transcript: 'Hey TARS',
          replayed: false,
          audio_chunks_base64: ['UklGRg=='],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      );
    });

    const response = await audioService.submitUtterance(
      wav,
      'http://127.0.0.1:8000',
      'conv-1',
      'ptt-conv-1',
      'turn-1'
    );

    expect(fetchSpy.mock.calls[0][0]).toBe('http://127.0.0.1:8000/api/v1/voice/utterance');
    expect(fetchSpy.mock.calls[0][1]).toEqual(
      expect.objectContaining({
        method: 'POST',
        headers: { 'X-TARS-Turn-ID': 'turn-1' },
      })
    );
    expect(submitted.current?.get('file')).toBeInstanceOf(Blob);
    expect(submitted.current?.get('turn_id')).toBe('turn-1');
    expect(submitted.current?.get('session_id')).toBe('ptt-conv-1');
    expect(response.status).toBe('awaiting_command');
  });

  it('plays backend-produced chunks without another synthesis request', async () => {
    const playSpy = vi.spyOn(audioService, 'playAudioBytes').mockResolvedValue();
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const first = btoa(String.fromCharCode(1, 2, 3));
    const second = btoa(String.fromCharCode(4, 5));

    await audioService.playBase64Chunks([first, second]);

    expect(playSpy).toHaveBeenCalledTimes(2);
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
