import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { MemoryView } from '../components/memory/MemoryView';
import { ActiveSetupsView } from '../components/setups/ActiveSetupsView';
import { TARSTradingEvent } from '../types/trading-event';

describe('Metrics & Memory Boundaries Verification', () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('renders clean grounded empty state in real mode with zero fake performance metrics', () => {
    render(<MemoryView apiEndpoint="http://127.0.0.1:8000" mockModeActive={false} />);

    // Must not contain fabricated metrics
    expect(screen.queryByText(/Sharpe/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/DSR/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/win rate/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/expectancy/i)).not.toBeInTheDocument();

    // Must display real mode grounded state message
    expect(screen.getByText(/Real Backend Mode active\. Grounded records only\./i)).toBeInTheDocument();
  });

  it('labels demo fixtures with [DEMO/MOCK] and DEMO tag only when mock mode is explicitly enabled', () => {
    render(<MemoryView apiEndpoint="http://127.0.0.1:8000" mockModeActive={true} />);

    // In mock mode, demo fixture items are displayed and explicitly labeled
    expect(screen.getByText(/\[DEMO\/MOCK\] H4 Orderblock & FVG Confluence Rules/i)).toBeInTheDocument();
    expect(screen.getByText(/\[DEMO\/MOCK\] Live Session Risk Allocation Limits/i)).toBeInTheDocument();

    const demoBadges = screen.getAllByText('#DEMO');
    expect(demoBadges.length).toBeGreaterThan(0);

    // Must NOT contain fabricated performance claims even in demo fixtures
    expect(screen.queryByText(/Realized Sharpe/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Verified DSR/i)).not.toBeInTheDocument();
  });

  it('renders ActiveSetupsView with grounded deterministic parameters only', () => {
    const groundedSetup: TARSTradingEvent = {
      schema_version: '1.0.0',
      event_id: 'grounded-evt-1',
      timestamp: '2026-08-16T10:00:00Z',
      source: 'quant_brain',
      symbol: 'XAUUSD',
      strategy_id: 'strat_h4_orderblock',
      state: 'SETUP_VALID',
      direction: 'LONG',
      entry: 2684.50,
      stop_loss: 2676.00,
      take_profit: 2708.50,
      risk_reward: 2.82,
      risk_percent: 1.0,
      validation_status: 'VALID',
      reason_codes: ['H4_ORDER_BLOCK_TAP', 'M15_FVG_CONFLUENCE']
    };

    const onSelect = vi.fn();
    render(<ActiveSetupsView setups={[groundedSetup]} onSelectSetup={onSelect} />);

    expect(screen.getByText('XAUUSD')).toBeInTheDocument();
    expect(screen.getByText('2684.5')).toBeInTheDocument();
    expect(screen.getByText('2676')).toBeInTheDocument();
    expect(screen.getByText('2708.5')).toBeInTheDocument();
    expect(screen.getByText('2.82R (1%)')).toBeInTheDocument();

    // Verify zero fabricated confidence percentages or invented metrics
    expect(screen.queryByText(/AI Confidence/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Win Probability/i)).not.toBeInTheDocument();
  });
});
