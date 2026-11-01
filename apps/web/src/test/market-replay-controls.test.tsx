import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../marketxray/api';
import { MarketXRayApp, PLAY_STEP_MS } from '../marketxray/MarketXRayApp';
import { CurrencyStrengthPanel } from '../marketxray/components/Panels';
import { REPLAY_WINDOW, ReplayControls } from '../marketxray/components/ReplayControls';
import { StatusBanner, TopNav, sourceName } from '../marketxray/components/TopNav';
import type { Dashboard, MarketStatus, Provenance, StrengthSnapshot } from '../marketxray/types';

const status = {
  status: 'REPLAY',
  market_open: false,
  as_of: '2026-11-01T00:00:00+00:00',
  data_as_of: REPLAY_WINDOW.start,
  data_age_seconds: 1,
  sessions_active: [],
  next_transition: null,
  detail: 'Replaying recorded history. Not a live feed.',
  replay: true,
  source: 'RECORDED_STORE',
  feed_connection: 'NOT_CONFIGURED',
  feed_detail: ''
} as unknown as MarketStatus;

const dukascopy: Provenance = {
  source: 'RECORDED_STORE',
  provider: 'dukascopy',
  observed_at: REPLAY_WINDOW.start,
  replay: true,
  detail: 'Reconstructed analysis, computed now from Dukascopy candles.',
  is_live: false
};

let cursor = REPLAY_WINDOW.start;
let last = '2026-10-29T00:00:00+00:00';

const dashboard = (): Dashboard =>
  ({
    status,
    cursor: { as_of: cursor, first_bar: '2026-01-01T00:00:00+00:00', last_bar: last },
    timeframe: 'H1',
    timeframes: ['H1'],
    selected_symbol: 'XAUUSD',
    instruments: [],
    xray: { timeframe: 'H1', return_window: 12, as_of: cursor, series: [], skipped: {}, measures: '' },
    strength: { window: '1D', timeframe: 'H1', bars: 24, as_of: cursor, ranking: [], pairs_evaluated: [] },
    leaders: { timeframe: 'H1', as_of: cursor, bullish: [], bearish: [], measures: '' },
    chart: { symbol: 'XAUUSD', timeframe: 'H1', as_of: cursor, candles: [] },
    order_blocks: { zones: [], active_count: 0 },
    events: { events: [], total_stored: 0, zones_stored: 0 },
    config: {},
    provenance: dukascopy,
    replay_pinned: true
  }) as unknown as Dashboard;

const addHour = (iso: string, bars: number): string =>
  new Date(Date.parse(iso) + bars * 3600_000).toISOString().replace('.000Z', '+00:00');

describe('Replay controls', () => {
  const noop = () => undefined;
  const controls = (overrides = {}) => (
    <ReplayControls
      cursor={REPLAY_WINDOW.start}
      timeframe="H1"
      wsLive
      stepping={false}
      playing={false}
      windowCovered
      onStep={noop}
      onLatest={noop}
      onTogglePlay={noop}
      onSeek={noop}
      onOpenWindow={noop}
      {...overrides}
    />
  );

  it('shows the cursor in UTC and IST', () => {
    render(controls());
    expect(document.body.textContent).toContain('2026-10-27 10:00 UTC · 15:30 IST');
  });

  it('seeks to the time entered, in UTC', () => {
    const onSeek = vi.fn();
    render(controls({ onSeek }));
    fireEvent.change(screen.getByLabelText('Seek to UTC time'), { target: { value: '2026-10-27T12:30' } });
    fireEvent.click(screen.getByRole('button', { name: 'seek UTC' }));
    expect(onSeek).toHaveBeenCalledWith('2026-10-27T12:30:00+00:00');
  });

  it('will not offer a window the store has no data for', () => {
    render(controls({ windowCovered: false }));
    const button = screen.getByRole('button', { name: /XAUUSD · 27 Oct/ }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.title).toMatch(/no data for this date/);
  });

  it('keeps pause available while playing and locks the other controls', () => {
    render(controls({ playing: true }));
    expect((screen.getByRole('button', { name: /pause/ }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByRole('button', { name: 'Next candle' }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe('Replay on the dashboard', () => {
  beforeEach(() => {
    cursor = REPLAY_WINDOW.start;
    last = '2026-10-29T00:00:00+00:00';
    localStorage.clear();
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
    vi.stubGlobal(
      'WebSocket',
      class {
        close() {}
        send() {}
      }
    );
    vi.spyOn(api, 'setup').mockResolvedValue({ ready: true } as never);
    vi.spyOn(api, 'dashboard').mockImplementation(async () => dashboard());
    vi.spyOn(api, 'step').mockImplementation(async (_tf, bars) => {
      const next = addHour(cursor, bars);
      cursor = Date.parse(next) > Date.parse(last) ? last : next;
      return { cursor, status };
    });
    vi.spyOn(api, 'seek').mockImplementation(async (iso) => {
      cursor = iso;
      return { cursor, status };
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  const loaded = () => screen.findByRole('button', { name: /play/ });

  it('next candle moves one bar and redraws', async () => {
    render(<MarketXRayApp />);
    await loaded();
    fireEvent.click(screen.getByRole('button', { name: 'Next candle' }));
    await waitFor(() => expect(api.step).toHaveBeenCalledWith('H1', 1));
    await waitFor(() => expect(document.body.textContent).toContain('2026-10-27 11:00 UTC'));
  });

  it('opens the window on XAUUSD H1 at its first moment', async () => {
    cursor = '2026-10-28T23:00:00+00:00';
    render(<MarketXRayApp />);
    await loaded();
    fireEvent.click(screen.getByRole('button', { name: /XAUUSD · 27 Oct/ }));
    await waitFor(() => expect(api.seek).toHaveBeenCalledWith(REPLAY_WINDOW.start));
    await waitFor(() =>
      expect(api.dashboard).toHaveBeenLastCalledWith('H1', 'XAUUSD', expect.anything())
    );
  });

  it('plays one candle at a time and stops at the end of the window', async () => {
    render(<MarketXRayApp />);
    await loaded();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fireEvent.click(screen.getByRole('button', { name: /play/ }));
    for (let i = 0; i < 8; i += 1) {
      await act(() => vi.advanceTimersByTimeAsync(PLAY_STEP_MS));
    }
    expect(api.step).toHaveBeenCalledTimes(4);
    expect(cursor).toBe(REPLAY_WINDOW.end);
    expect(screen.getByRole('button', { name: /play/ })).toBeTruthy();
  });

  it('stops stepping as soon as it is paused', async () => {
    render(<MarketXRayApp />);
    await loaded();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fireEvent.click(screen.getByRole('button', { name: /play/ }));
    await act(() => vi.advanceTimersByTimeAsync(PLAY_STEP_MS));
    expect(api.step).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: /pause/ }));
    await act(() => vi.advanceTimersByTimeAsync(PLAY_STEP_MS * 5));
    expect(api.step).toHaveBeenCalledTimes(1);
  });

  it('stops at the end of the data instead of stepping forever', async () => {
    cursor = addHour(last, -1);
    render(<MarketXRayApp />);
    await loaded();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fireEvent.click(screen.getByRole('button', { name: /play/ }));
    for (let i = 0; i < 6; i += 1) {
      await act(() => vi.advanceTimersByTimeAsync(PLAY_STEP_MS));
    }
    expect(api.step).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('button', { name: /play/ })).toBeTruthy();
  });

  it('says so when a seek lands somewhere else', async () => {
    vi.mocked(api.seek).mockImplementation(async () => ({ cursor: last, status }));
    render(<MarketXRayApp />);
    await loaded();
    fireEvent.change(screen.getByLabelText('Seek to UTC time'), { target: { value: '2027-01-01T00:00' } });
    fireEvent.click(screen.getByRole('button', { name: 'seek UTC' }));
    await waitFor(() => expect(document.body.textContent).toContain('has no data at that moment'));
  });

  it('reports a failed step and stops playing', async () => {
    vi.mocked(api.step).mockRejectedValue(new Error('backend gone'));
    render(<MarketXRayApp />);
    await loaded();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fireEvent.click(screen.getByRole('button', { name: /play/ }));
    await act(() => vi.advanceTimersByTimeAsync(PLAY_STEP_MS));
    expect(document.body.textContent).toContain('Replay step failed.');
    expect(screen.getByRole('button', { name: /play/ })).toBeTruthy();
  });

  it('every section tab goes to a panel that exists', async () => {
    const scrolled: string[] = [];
    HTMLElement.prototype.scrollIntoView = function scroll(this: HTMLElement) {
      scrolled.push(this.id);
    };
    render(<MarketXRayApp />);
    await loaded();
    for (const name of ['Order Blocks', 'Scanner', 'Journal', 'Market X-Ray']) {
      fireEvent.click(screen.getByRole('button', { name }));
    }
    expect(scrolled).toEqual([
      'mx-section-blocks',
      'mx-section-scanner',
      'mx-section-journal',
      'mx-section-xray'
    ]);
  });
});

describe('A store holding one instrument', () => {
  const empty = {
    window: '1D',
    timeframe: 'H1',
    bars: 24,
    as_of: REPLAY_WINDOW.start,
    pairs_evaluated: [],
    // What the backend sends with no pairs: every currency, each scored from nothing.
    ranking: ['USD', 'EUR'].map((currency) => ({
      currency,
      score: 0,
      pairs_used: 0,
      counterparties: [],
      leave_one_out_max_delta: 0,
      leave_one_out_driver: null,
      thin_coverage: true
    }))
  } as unknown as StrengthSnapshot;

  it('says strength is unavailable rather than showing scores of zero', () => {
    render(<CurrencyStrengthPanel snapshot={empty} window="1D" windows={['1D']} onWindow={() => undefined} />);
    expect(document.body.textContent).toMatch(/unavailable/i);
    expect(document.body.textContent).toMatch(/no FX pairs/i);
    expect(document.body.textContent).not.toContain('+0.00');
    expect(document.querySelector('.mx-ccy-row')).toBeNull();
  });

  it('describes the real coverage and the rankings it cannot give', async () => {
    localStorage.clear();
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
    vi.stubGlobal('WebSocket', class { close() {} send() {} });
    vi.spyOn(api, 'setup').mockResolvedValue({ ready: true } as never);
    vi.spyOn(api, 'dashboard').mockResolvedValue({
      ...dashboard(),
      strength: empty,
      instruments: [{ symbol: 'XAUUSD', asset_class: 'METAL', price_precision: 3 }]
    } as unknown as Dashboard);
    render(<MarketXRayApp />);
    await screen.findByText(/Cross-instrument rankings are unavailable/);
    expect(document.body.textContent).not.toContain('17 FX pairs');
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });
});

describe('Where replayed data came from', () => {
  it('names the vendor, and never upgrades it', () => {
    render(<StatusBanner status={status} provenance={dukascopy} replayPinned />);
    expect(screen.getByText('DUKASCOPY (HISTORICAL)')).toBeTruthy();
    expect(screen.getByText(/RECONSTRUCTED ANALYSIS · NOT A RECORDED PREDICTION/)).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/OANDA|TradingView/i);
    expect(screen.queryByText(/^LIVE FEED/)).toBeNull();
  });

  it('claims no vendor for a store that names none', () => {
    const store = { ...dukascopy, provider: 'parquet_store' };
    expect(sourceName(store)).toBeNull();
    render(<StatusBanner status={status} provenance={store} />);
    expect(screen.getByText('RECORDED STORE (HISTORICAL)')).toBeTruthy();
    expect(screen.queryByText(/RECONSTRUCTED/)).toBeNull();
  });

  it('labels the nav with the vendor while pinned', () => {
    render(
      <TopNav status={status} provenance={dukascopy} replayPinned section="xray" onSection={() => undefined} />
    );
    expect(screen.getByText('REPLAY PINNED · DUKASCOPY')).toBeTruthy();
  });
});
