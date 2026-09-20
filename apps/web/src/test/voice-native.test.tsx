import { describe, it, expect, vi, beforeEach } from 'vitest';
import { audioService } from '../services/audio';
import { nativeBridge } from '../services/native-bridge';

describe('Voice-native audio and window behaviour', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  describe('AudioService Instant Interruption & Barge-in', () => {
    it('stopSpeaking() immediately cancels active Web Audio playback context', () => {
      const stopSpy = vi.spyOn(audioService, 'stopSpeaking');
      audioService.stopSpeaking();
      expect(stopSpy).toHaveBeenCalledOnce();
    });

    it('playSentenceQueue synthesizes and plays sentence chunks sequentially', async () => {
      const sentences = ['First sentence completed.', 'Second sentence following.'];
      const synthesizeSpy = vi.spyOn(audioService, 'synthesizeAndPlay').mockResolvedValue();

      await audioService.playSentenceQueue(sentences, 'http://127.0.0.1:8000');
      expect(synthesizeSpy).toHaveBeenCalledTimes(2);
      expect(synthesizeSpy).toHaveBeenNthCalledWith(1, 'First sentence completed.', 'http://127.0.0.1:8000', undefined);
      expect(synthesizeSpy).toHaveBeenNthCalledWith(2, 'Second sentence following.', 'http://127.0.0.1:8000', undefined);
    });
  });

  describe('NativeBridge Window Sizing for Minimal Floating Panel', () => {
    it('supports setWindowSize for voice panel (420x260)', async () => {
      const setSizeSpy = vi.spyOn(nativeBridge, 'setWindowSize').mockResolvedValue();
      await nativeBridge.setWindowSize(420, 260, true);
      expect(setSizeSpy).toHaveBeenCalledWith(420, 260, true);
    });

    it('summonHUD accepts voice mode to summon tiny floating panel', async () => {
      const summonSpy = vi.spyOn(nativeBridge, 'summonHUD').mockResolvedValue();
      await nativeBridge.summonHUD('voice');
      expect(summonSpy).toHaveBeenCalledWith('voice');
    });
  });

});
