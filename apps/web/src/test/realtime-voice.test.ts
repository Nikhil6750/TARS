import { afterEach, describe, expect, it, vi } from 'vitest';
import { AudioService, audioService } from '../services/audio';
import { RealtimeVoiceClient } from '../runtime/RealtimeVoiceClient';

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('realtime voice transport cancellation', () => {
  it('flushes queued old audio and rejects late packets when a new turn owns playback', async () => {
    let release: (() => void) | undefined;
    const play = vi.spyOn(audioService, 'playAudioBytes').mockImplementationOnce(
      () => new Promise<void>(resolve => { release = resolve; })
    ).mockResolvedValue();
    vi.spyOn(audioService, 'stopSpeaking').mockImplementation(() => release?.());
    const client = new RealtimeVoiceClient();
    const audio = btoa('wav');
    client.accept({ type: 'audio_chunk', turn_id: 'old', generation: 1, audio, sequence: 1 });
    client.accept({ type: 'audio_chunk', turn_id: 'old', generation: 1, audio, sequence: 2 });
    expect(play).toHaveBeenCalledTimes(1);
    client.accept({ type: 'interrupt', turn_id: 'new', previous_turn_id: 'old', generation: 2 });
    client.accept({ type: 'audio_chunk', turn_id: 'old', generation: 1, audio, sequence: 3 });
    client.accept({ type: 'audio_chunk', turn_id: 'new', generation: 2, audio, sequence: 4 });
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(play).toHaveBeenCalledTimes(2);
    client.stop();
  });

  it('cannot restart an AudioBufferSource whose asynchronous decode finishes after cancellation', async () => {
    let decoded: ((value: unknown) => void) | undefined;
    const createSource = vi.fn();
    class Context {
      state = 'running';
      decodeAudioData(_bytes: ArrayBuffer, callback: (value: unknown) => void) { decoded = callback; }
      createBufferSource = createSource;
      close() { this.state = 'closed'; return Promise.resolve(); }
    }
    vi.stubGlobal('AudioContext', Context);
    const service = new AudioService();
    const playing = service.playAudioBytes(new ArrayBuffer(4));
    service.stopSpeaking();
    decoded?.({});
    await playing;
    expect(createSource).not.toHaveBeenCalled();
  });
});
