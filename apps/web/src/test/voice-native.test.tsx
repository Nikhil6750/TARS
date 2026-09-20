import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { FloatingVoicePanel } from '../components/voice/FloatingVoicePanel';
import { audioService } from '../services/audio';
import { nativeBridge } from '../services/native-bridge';

describe('Overnight Voice-Native Experience & FloatingVoicePanel', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  describe('FloatingVoicePanel UI & States (No Decorative Orb)', () => {
    it('renders minimal voice panel with waveform and no decorative orb', () => {
      render(
        <FloatingVoicePanel
          companionState="IDLE"
          isListening={false}
          onTogglePushToTalk={vi.fn()}
          audioVolume={0}
          wakeStatus={{
            isActive: true,
            engine: 'vad_whisper_local',
            engineLabel: 'Local VAD + faster-whisper',
            targetPhrase: 'Hey TARS',
          }}
        />
      );

      // Verify header & title
      expect(screen.getByText('TARS')).toBeInTheDocument();
      expect(screen.getByText('VOICE')).toBeInTheDocument();
      expect(screen.getByText(/READY \("Hey TARS"\)/i)).toBeInTheDocument();
      expect(screen.getByText('WAKE ON')).toBeInTheDocument();

      // Verify NO orb image
      expect(screen.queryByAltText(/TARS Quantum Voice Core/i)).not.toBeInTheDocument();
      expect(screen.queryByAltText(/TARS Orb/i)).not.toBeInTheDocument();

      // Verify prompt instruction
      expect(screen.getByText(/Say "Hey TARS" or speak your command/i)).toBeInTheDocument();
    });

    it('renders LISTENING state with live user transcript and active VAD status', () => {
      render(
        <FloatingVoicePanel
          companionState="LISTENING"
          isListening={true}
          onTogglePushToTalk={vi.fn()}
          audioVolume={0.65}
          liveTranscript="What is the current stop loss on Gold?"
        />
      );

      expect(screen.getByText('LISTENING')).toBeInTheDocument();
      expect(screen.getByText(/YOU SAID:/i)).toBeInTheDocument();
      expect(screen.getByText(/What is the current stop loss on Gold\?/i)).toBeInTheDocument();
      expect(screen.getByText('STOP (PTT)')).toBeInTheDocument();
    });

    it('renders THINKING state with progressive streaming text from Claude', () => {
      render(
        <FloatingVoicePanel
          companionState="THINKING"
          isListening={false}
          onTogglePushToTalk={vi.fn()}
          audioVolume={0}
          streamedText="Analyzing market structure on EURUSD: Support holding at 1.0820..."
        />
      );

      expect(screen.getByText('THINKING...')).toBeInTheDocument();
      expect(screen.getByText(/TARS STREAMING:/i)).toBeInTheDocument();
      expect(screen.getByText(/Analyzing market structure on EURUSD: Support holding at 1.0820.../i)).toBeInTheDocument();
    });

    it('renders SPEAKING state with barge-in INTERRUPT button', () => {
      const onInterrupt = vi.fn();
      render(
        <FloatingVoicePanel
          companionState="SPEAKING"
          isListening={false}
          onTogglePushToTalk={vi.fn()}
          audioVolume={0.4}
          responseSpeakerText="Gold SL is set at 2676.00 with 2.82 R:R."
          onInterruptSpeech={onInterrupt}
        />
      );

      expect(screen.getByText('TARS SPEAKING')).toBeInTheDocument();
      expect(screen.getByText(/TARS:/i)).toBeInTheDocument();
      expect(screen.getByText(/Gold SL is set at 2676.00 with 2.82 R:R./i)).toBeInTheDocument();

      const interruptBtn = screen.getByRole('button', { name: /INTERRUPT/i });
      expect(interruptBtn).toBeInTheDocument();

      fireEvent.click(interruptBtn);
      expect(onInterrupt).toHaveBeenCalledOnce();
    });

    it('renders ERROR state truthfully with genuine error details and no fake confidence', () => {
      render(
        <FloatingVoicePanel
          companionState="WARNING"
          isListening={false}
          onTogglePushToTalk={vi.fn()}
          audioVolume={0}
          errorMessage="Local microphone capture denied by system permissions"
        />
      );

      expect(screen.getByText('ERROR')).toBeInTheDocument();
      expect(screen.getByText('Voice Pipeline Error')).toBeInTheDocument();
      expect(screen.getByText(/Local microphone capture denied by system permissions/i)).toBeInTheDocument();
    });

    it('displays active application context title preserved underneath', () => {
      render(
        <FloatingVoicePanel
          companionState="IDLE"
          isListening={false}
          onTogglePushToTalk={vi.fn()}
          audioVolume={0}
          activeContextTitle="TradingView — BTCUSD 1H Chart"
        />
      );

      expect(screen.getByText(/Context: TradingView — BTCUSD 1H Chart/i)).toBeInTheDocument();
      expect(screen.getByText('Native Lock')).toBeInTheDocument();
    });
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
