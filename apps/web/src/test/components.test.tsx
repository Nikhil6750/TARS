import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { TARSCharacter } from '../components/character/TARSCharacter';
import { ActiveSetupsView } from '../components/setups/ActiveSetupsView';
import { CompactHUD } from '../components/navigation/CompactHUD';
import { TARSTradingEvent } from '../types/trading-event';

describe('UI Components', () => {
  afterEach(() => {
    cleanup();
  });

  it('renders TARSCharacter across mood states', () => {
    const { rerender } = render(<TARSCharacter state="IDLE" size="hero" />);
    expect(screen.getByText('READY')).toBeInTheDocument();

    rerender(<TARSCharacter state="LISTENING" size="hero" audioVolume={0.5} />);
    expect(screen.getByText('LISTENING')).toBeInTheDocument();

    rerender(<TARSCharacter state="ALERT" size="hero" />);
    expect(screen.getByText('SETUP ALERT')).toBeInTheDocument();

    rerender(<TARSCharacter state="WARNING" size="hero" />);
    expect(screen.getByText('RISK DETECTED')).toBeInTheDocument();
  });

  it('renders ActiveSetupsView and handles filtering and inspection', () => {
    const mockSetups: TARSTradingEvent[] = [
      {
        schema_version: '1.0.0',
        event_id: 'e1',
        timestamp: '2026-08-16T10:00:00Z',
        source: 'mock',
        symbol: 'XAUUSD',
        strategy_id: 'strat_ob_v1',
        state: 'SETUP_VALID',
        direction: 'LONG',
        entry: 2680.0,
        stop_loss: 2670.0,
        take_profit: 2700.0,
        risk_reward: 2.0,
        risk_percent: 1.0,
        validation_status: 'VALID',
        reason_codes: ['ORDER_BLOCK_TAP']
      },
      {
        schema_version: '1.0.0',
        event_id: 'e2',
        timestamp: '2026-08-16T10:05:00Z',
        source: 'mock',
        symbol: 'BTCUSD',
        strategy_id: 'strat_mr_v1',
        state: 'SETUP_INVALIDATED',
        direction: 'SHORT',
        entry: 96000.0,
        stop_loss: 97000.0,
        take_profit: 94000.0,
        risk_reward: 2.0,
        risk_percent: 0.5,
        validation_status: 'INVALID',
        reason_codes: ['INVALIDATION_SWEEP']
      }
    ];

    const onSelect = vi.fn();
    render(<ActiveSetupsView setups={mockSetups} onSelectSetup={onSelect} />);

    expect(screen.getByText('XAUUSD')).toBeInTheDocument();
    expect(screen.getByText('BTCUSD')).toBeInTheDocument();

    // Click on XAUUSD setup card
    fireEvent.click(screen.getByText('XAUUSD'));
    expect(onSelect).toHaveBeenCalledWith(mockSetups[0]);

    // Search filter test
    const searchInput = screen.getByPlaceholderText('Search symbol...');
    fireEvent.change(searchInput, { target: { value: 'BTC' } });
    expect(screen.queryByText('XAUUSD')).not.toBeInTheDocument();
    expect(screen.getByText('BTCUSD')).toBeInTheDocument();
  });

  it('renders CompactHUD with quick spotlight and expand button', () => {
    const mockSetups: TARSTradingEvent[] = [
      {
        schema_version: '1.0.0',
        event_id: 'e1',
        timestamp: '2026-08-16T10:00:00Z',
        source: 'mock',
        symbol: 'ES',
        state: 'SETUP_VALID',
        direction: 'LONG',
        entry: 5880.0,
        stop_loss: 5865.0,
        take_profit: 5920.0,
        risk_reward: 2.67,
        validation_status: 'VALID'
      }
    ];

    const onExpand = vi.fn();
    const onTogglePtt = vi.fn();

    render(
      <CompactHUD
        companionState="IDLE"
        onExpand={onExpand}
        activeSetups={mockSetups}
        criticalWarnings={['Macro CPI alert']}
        isListening={false}
        onTogglePushToTalk={onTogglePtt}
        audioVolume={0}
      />
    );

    expect(screen.getByText('TARS HUD')).toBeInTheDocument();
    expect(screen.getByText('ES')).toBeInTheDocument();
    expect(screen.getByText('HOLD TO TALK')).toBeInTheDocument();
  });
});
