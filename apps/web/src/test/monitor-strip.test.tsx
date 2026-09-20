import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { MonitorStrip } from '../components/voice/MonitorStrip';
import { MonitorStatus } from '../services/monitors';

const base: MonitorStatus = {
  mt5: { state: 'NOT INSTALLED', detail: 'MetaTrader5 package is not installed' },
  tradingview: { state: 'NOT FOUND' },
  calendar: { state: 'CONNECTED' },
  news: { state: 'NOT CONFIGURED' },
  quote: null,
  replay: false,
};

describe('MonitorStrip truthfulness', () => {
  it('never claims a live quote when none exists', () => {
    render(<MonitorStrip status={base} alert={null} />);
    expect(screen.getByText('No live quote')).toBeTruthy();
    expect(screen.getByTitle(/MT5: NOT INSTALLED/)).toBeTruthy();
    expect(screen.getByTitle(/TV: NOT FOUND/)).toBeTruthy();
  });

  it('labels replay quotes and shows analysis on the alert', () => {
    const status = { ...base, quote: { symbol: 'EURUSD', bid: 1.0812, ask: 1.0814, source: 'DEMO REPLAY' }, replay: true };
    render(<MonitorStrip status={status} alert={{ title: '[DEMO REPLAY] NFP', summary: 's', analysis: 'USD strength', replay: true, at: 1 }} />);
    expect(screen.getByText('DEMO REPLAY')).toBeTruthy();
    expect(screen.getByText('USD strength')).toBeTruthy();
  });

  it('renders unknown status as unknown, not connected', () => {
    render(<MonitorStrip status={null} alert={null} />);
    expect(screen.getByTitle(/MT5: UNKNOWN/)).toBeTruthy();
  });
});
