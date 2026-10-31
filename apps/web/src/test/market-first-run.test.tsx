import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { FirstRun } from '../marketxray/components/FirstRun';
import { api } from '../marketxray/api';
import type { MarketStatus, SetupStatus } from '../marketxray/types';

const status = (feed: MarketStatus['feed_connection']): MarketStatus =>
  ({
    status: 'NO_DATA',
    market_open: false,
    as_of: '2026-10-31T00:00:00+00:00',
    data_as_of: null,
    data_age_seconds: null,
    sessions_active: [],
    next_transition: null,
    detail: 'No market history yet.',
    replay: false,
    feed_connection: feed
  }) as MarketStatus;

const setup = (overrides: Partial<SetupStatus> = {}): SetupStatus => ({
  ready: false,
  store_root: 'D:/data/market/xray_store',
  symbols_in_store: 0,
  sources: {
    csv_root: 'C:/Trade Data',
    csv_root_exists: true,
    instruments_found: ['EURUSD', 'XAUUSD'],
    instruments_missing: ['GBPUSD']
  },
  import: { state: 'IDLE', error: null },
  status: status('NOT_CONFIGURED'),
  ...overrides
});

describe('Market first-run screen', () => {
  afterEach(() => vi.restoreAllMocks());

  it('says what is missing and what was found, without a terminal command', () => {
    render(<FirstRun setup={setup()} onChanged={() => undefined} />);
    expect(screen.getByText('No market history yet')).toBeTruthy();
    expect(screen.getByText(/2 of 3 supported instruments/)).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/python -m/);
  });

  it('cannot start an import with nothing to import', () => {
    const empty = setup({
      sources: { csv_root: 'Z:/x', csv_root_exists: false, instruments_found: [], instruments_missing: ['EURUSD'] }
    });
    render(<FirstRun setup={empty} onChanged={() => undefined} />);
    const button = screen.getByRole('button', { name: /import 0/ }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(screen.getByText(/does not exist on this machine/)).toBeTruthy();
  });

  it('runs the import for the chosen folder and reports progress', async () => {
    const start = vi.spyOn(api, 'startImport').mockResolvedValue({ state: 'RUNNING', error: null });
    const running = setup({ import: { state: 'RUNNING', error: null } });
    vi.spyOn(api, 'setup').mockResolvedValue(running);
    const onChanged = vi.fn();
    render(<FirstRun setup={setup()} onChanged={onChanged} />);
    fireEvent.change(screen.getByLabelText(/Folder containing/), { target: { value: 'E:/csvs' } });
    fireEvent.click(screen.getByRole('button', { name: /import 2 instruments/ }));
    await waitFor(() => expect(start).toHaveBeenCalledWith('E:/csvs'));
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(running));
  });

  it('shows a failed import instead of hiding it', () => {
    const failed = setup({ import: { state: 'FAILED', error: 'no bid/ask CSV pairs found' } });
    render(<FirstRun setup={failed} onChanged={() => undefined} />);
    expect(screen.getByText(/Import failed: no bid\/ask CSV pairs found/)).toBeTruthy();
  });

  it('tells an unauthenticated user to log in themselves, and never asks for credentials', () => {
    render(<FirstRun setup={setup({ status: status('AUTH_REQUIRED') })} onChanged={() => undefined} />);
    expect(screen.getByText(/no account is logged in/)).toBeTruthy();
    expect(document.querySelector('input[type="password"]')).toBeNull();
  });
});
