import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { TARSOrb } from '../components/character/TARSOrb';
import { ChartAnalysisCard, ChartAnalysisData } from '../components/hud/ChartAnalysisCard';
import { HUDOverlay } from '../components/hud/HUDOverlay';

describe('LinkedIn Demo UI & Wake Experience Tests', () => {
  describe('TARSOrb Component State Visuals', () => {
    it('renders TARSOrb in IDLE state with core asset and idle status', () => {
      render(<TARSOrb state="IDLE" audioVolume={0} size="hud" />);
      expect(screen.getByRole('button', { name: /TARS Voice Companion Orb, state: IDLE/i })).toBeInTheDocument();
      expect(screen.getByText(/TARS IDLE/i)).toBeInTheDocument();
      const img = screen.getByAltText('TARS Quantum Voice Core');
      expect(img).toBeInTheDocument();
      expect(img).toHaveAttribute('src', '/assets/tars-orb.png');
    });

    it('renders TARSOrb in WAKE state with wake status', () => {
      render(<TARSOrb state="WAKE" audioVolume={0} size="hud" />);
      expect(screen.getByRole('button', { name: /TARS Voice Companion Orb, state: WAKE/i })).toBeInTheDocument();
      expect(screen.getByText(/WAKING/i)).toBeInTheDocument();
    });

    it('renders TARSOrb in LISTENING state reacting to real audio amplitude', () => {
      render(<TARSOrb state="LISTENING" audioVolume={0.75} size="hud" />);
      expect(screen.getByRole('button', { name: /TARS Voice Companion Orb, state: LISTENING/i })).toBeInTheDocument();
      expect(screen.getByText(/LISTENING/i)).toBeInTheDocument();
    });

    it('renders TARSOrb in THINKING and SPEAKING states', () => {
      const { rerender } = render(<TARSOrb state="THINKING" audioVolume={0} size="hud" />);
      expect(screen.getByText(/ANALYZING/i)).toBeInTheDocument();

      rerender(<TARSOrb state="SPEAKING" audioVolume={0.6} size="hud" />);
      expect(screen.getByText(/TRANSMITTING/i)).toBeInTheDocument();
    });
  });

  describe('ChartAnalysisCard Presentation', () => {
    const sampleAnalysis: ChartAnalysisData = {
      instrument: 'EURUSD',
      timeframe: '4H',
      market_context: 'Price action consolidating near key 1.0850 daily resistance.',
      key_levels: ['1.0850 Resistance', '1.0780 Support'],
      possible_setup: 'Bullish flag breakout if 1.0855 clears with volume.',
      invalidation: 'Break below 1.0780 invalidates bullish continuation bias.',
      risk_notes: 'High-impact US CPI release in 2 hours adds elevated volatility.',
      provider: 'Claude Code',
      disclaimer: 'Qualitative read from TARS assistant. Not a quant_brain signal. No confidence score.',
      speech_text: 'Looking at EURUSD 4H. Price action consolidating near resistance.',
    };

    it('renders structured chart analysis correctly with key levels and non-negotiable disclaimer', () => {
      const onDismiss = vi.fn();
      const onReplayAudio = vi.fn();

      render(
        <ChartAnalysisCard
          analysis={sampleAnalysis}
          onDismiss={onDismiss}
          onReplayAudio={onReplayAudio}
          isSpeaking={false}
        />
      );

      expect(screen.getByText(/EURUSD · 4H/i)).toBeInTheDocument();
      expect(screen.getByText(/Price action consolidating/i)).toBeInTheDocument();
      expect(screen.getByText(/1.0850 Resistance/i)).toBeInTheDocument();
      expect(screen.getByText(/1.0780 Support/i)).toBeInTheDocument();
      expect(screen.getByText(/Bullish flag breakout/i)).toBeInTheDocument();
      expect(screen.getByText(/Break below 1.0780/i)).toBeInTheDocument();
      expect(screen.getByText(/High-impact US CPI/i)).toBeInTheDocument();
      expect(screen.getByText(/Qualitative read from TARS assistant/i)).toBeInTheDocument();

      // Audio replay button
      const replayBtn = screen.getByTitle('Speak / Replay Voice Read');
      fireEvent.click(replayBtn);
      expect(onReplayAudio).toHaveBeenCalledOnce();
    });
  });

  describe('HUDOverlay Demo Trigger & Local Wake Integration', () => {
    it('renders HUDOverlay with Analyze Active Chart button and calls handler on click', async () => {
      const onAnalyzeChart = vi.fn();

      await act(async () => {
        render(
          <HUDOverlay
            companionState="IDLE"
            onExpand={vi.fn()}
            activeSetups={[]}
            criticalWarnings={[]}
            isListening={false}
            onTogglePushToTalk={vi.fn()}
            audioVolume={0}
            onAnalyzeChart={onAnalyzeChart}
            wakeStatus={{
              isActive: true,
              engine: 'web_speech_local',
              engineLabel: 'Local Web Speech',
              targetPhrase: 'Hey TARS',
            }}
          />
        );
      });

      expect(screen.getByText(/WAKE: ON/i)).toBeInTheDocument();
      const analyzeBtn = screen.getByRole('button', { name: /ANALYZE ACTIVE CHART/i });
      expect(analyzeBtn).toBeInTheDocument();

      fireEvent.click(analyzeBtn);
      expect(onAnalyzeChart).toHaveBeenCalledOnce();
    });

    it('renders progressive live streaming text directly under the Orb when analyzing', async () => {
      await act(async () => {
        render(
          <HUDOverlay
            companionState="THINKING"
            onExpand={vi.fn()}
            activeSetups={[]}
            criticalWarnings={[]}
            isListening={false}
            onTogglePushToTalk={vi.fn()}
            audioVolume={0}
            isAnalyzingChart={true}
            streamedAnalysisText="Observing breakout above 1.0850 key level with elevated volume..."
          />
        );
      });

      expect(screen.getAllByText(/ANALYZING ACTIVE CHART.../i).length).toBeGreaterThan(0);
      expect(screen.getByText(/Observing breakout above 1.0850 key level/i)).toBeInTheDocument();
      expect(screen.getByText(/TARS AI · Live/i)).toBeInTheDocument();
    });

    it('matches all conversational chart analysis voice variants deterministically', () => {
      const CHART_REGEX = /\b(analy[sz]e|check|look\s+at|evaluate|read|scan|inspect|review|what\s+do\s+you\s+see\s+on)\s+(?:this|the|my|active|current)?\s*charts?\b/i;
      const variants = [
        'analyze this chart',
        'analyze the chart',
        'check this chart',
        'look at this chart',
        'can you analyze this chart',
        'evaluate the chart',
        'scan active chart',
        'what do you see on this chart',
      ];
      for (const variant of variants) {
        expect(CHART_REGEX.test(variant)).toBe(true);
      }
      expect(CHART_REGEX.test('what is the weather today')).toBe(false);
    });

  });
});
