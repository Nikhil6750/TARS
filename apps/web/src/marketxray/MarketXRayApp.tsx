/**
 * Market X-Ray dashboard shell.
 *
 * Loads one aggregate payload for the first paint, then keeps the live panels
 * current over the WebSocket. Selecting an instrument or changing timeframe
 * refetches only what that affects.
 *
 * The replay cursor lives on the backend, so the controls here just ask it to
 * move and re-read the result. That keeps the backend the single source of
 * truth: there is no client-side notion of "what time it is" that could drift
 * from the data being shown.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, WS_URL, api } from './api';
import { CandleChart } from './components/CandleChart';
import { CurrencyStrengthPanel, EventHistory, Panel, Segmented, ZoneTable, LeaderBoard } from './components/Panels';
import { StatusBanner, TopNav, type Section } from './components/TopNav';
import { isDesktop, loadPreferences, savePreferences, toggleFullscreen } from './desktop';
import { XRayCanvas } from './components/XRayCanvas';
import type {
  ChartPayload,
  Dashboard,
  EventsPayload,
  Instrument,
  LeadersSnapshot,
  MarketStatus,
  OrderBlocksPayload,
  StrengthSnapshot,
  XRaySnapshot
} from './types';

const TIMEFRAMES = ['M5', 'M15', 'H1', 'H4', 'D1'];
const STRENGTH_WINDOWS = ['1H', '4H', '1D'];

interface Fatal {
  message: string;
  remedy?: string;
}

export const MarketXRayApp: React.FC = () => {
  // Seeded from preferences stored locally on this machine, so reopening the
  // desktop window returns to the view that was last in use.
  const initialPrefs = useRef(loadPreferences());
  const [section, setSection] = useState<Section>('xray');
  const [timeframe, setTimeframe] = useState(initialPrefs.current.timeframe);
  const [strengthWindow, setStrengthWindow] = useState(initialPrefs.current.strengthWindow);
  const [symbol, setSymbol] = useState<string | null>(initialPrefs.current.symbol);
  const [showInvalidated, setShowInvalidated] = useState(initialPrefs.current.showInvalidated);
  const [fullscreen, setFullscreen] = useState(false);

  const [status, setStatus] = useState<MarketStatus | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [xray, setXray] = useState<XRaySnapshot | null>(null);
  const [strength, setStrength] = useState<StrengthSnapshot | null>(null);
  const [leaders, setLeaders] = useState<LeadersSnapshot | null>(null);
  const [chart, setChart] = useState<ChartPayload | null>(null);
  const [blocks, setBlocks] = useState<OrderBlocksPayload | null>(null);
  const [events, setEvents] = useState<EventsPayload | null>(null);

  const [fatal, setFatal] = useState<Fatal | null>(null);
  const [busy, setBusy] = useState(true);
  const [wsLive, setWsLive] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  // Guards against a slow earlier response overwriting a newer one.
  const requestSeq = useRef(0);

  const applyDashboard = useCallback((data: Dashboard) => {
    setStatus(data.status);
    setCursor(data.cursor.as_of);
    setInstruments(data.instruments);
    setXray(data.xray);
    setStrength(data.strength);
    setLeaders(data.leaders);
    setChart(data.chart);
    setBlocks(data.order_blocks);
    setEvents(data.events);
    setSymbol((current) => current ?? data.selected_symbol);
    setTimeframe(data.timeframe);
  }, []);

  const loadAll = useCallback(
    async (tf: string, sym: string | null, win: string) => {
      const seq = ++requestSeq.current;
      setBusy(true);
      try {
        const data = await api.dashboard(tf, sym ?? undefined, win);
        if (seq !== requestSeq.current) return;
        applyDashboard(data);
        setFatal(null);
      } catch (error) {
        if (seq !== requestSeq.current) return;
        if (error instanceof ApiError) {
          setFatal({ message: error.message, remedy: error.remedy });
        } else {
          setFatal({ message: String(error) });
        }
      } finally {
        if (seq === requestSeq.current) setBusy(false);
      }
    },
    [applyDashboard]
  );

  // First paint, using the stored preferences.
  useEffect(() => {
    const prefs = initialPrefs.current;
    void loadAll(prefs.timeframe, prefs.symbol, prefs.strengthWindow);
  }, [loadAll]);

  // Persist preferences whenever the view changes.
  useEffect(() => {
    savePreferences({ timeframe, symbol, strengthWindow, showInvalidated });
  }, [timeframe, symbol, strengthWindow, showInvalidated]);

  /** Refetch only the per-instrument panels. */
  const loadInstrument = useCallback(async (sym: string, tf: string) => {
    try {
      const [nextChart, nextBlocks, nextEvents] = await Promise.all([
        api.candles(sym, tf),
        api.orderBlocks(sym, tf),
        api.events(60)
      ]);
      setChart(nextChart);
      setBlocks(nextBlocks);
      setEvents(nextEvents);
      setFatal(null);
    } catch (error) {
      setNotice(
        error instanceof ApiError
          ? `Could not load ${sym}: ${error.message}`
          : `Could not load ${sym}.`
      );
    }
  }, []);

  const handleSelect = useCallback(
    (next: string) => {
      setSymbol(next);
      setNotice(null);
      void loadInstrument(next, timeframe);
    },
    [loadInstrument, timeframe]
  );

  const handleTimeframe = useCallback(
    (next: string) => {
      setTimeframe(next);
      void loadAll(next, symbol, strengthWindow);
    },
    [loadAll, symbol, strengthWindow]
  );

  const handleStrengthWindow = useCallback(
    (next: string) => {
      setStrengthWindow(next);
      api
        .strength(next)
        .then(setStrength)
        .catch(() => setNotice('Could not recompute currency strength.'));
    },
    []
  );

  // ---- replay controls ----
  const [stepping, setStepping] = useState(false);
  const replay = useCallback(
    async (action: 'back' | 'forward' | 'reset', bars = 1) => {
      setStepping(true);
      try {
        if (action === 'reset') await api.reset();
        else await api.step(timeframe, action === 'forward' ? bars : -bars);
        await loadAll(timeframe, symbol, strengthWindow);
      } catch (error) {
        setNotice(error instanceof ApiError ? error.message : 'Replay step failed.');
      } finally {
        setStepping(false);
      }
    },
    [timeframe, symbol, strengthWindow, loadAll]
  );

  // ---- websocket: keeps status/x-ray/strength/leaders current ----
  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry = 0;
    let timer = 0;
    let closed = false;

    const connect = () => {
      if (closed) return;
      try {
        socket = new WebSocket(WS_URL);
      } catch {
        schedule();
        return;
      }
      socket.onopen = () => {
        retry = 0;
        setWsLive(true);
        socket?.send(JSON.stringify({ type: 'subscribe', timeframe }));
      };
      socket.onmessage = (event) => {
        try {
          const frame = JSON.parse(event.data);
          if (frame.type === 'market.frame') {
            setStatus(frame.status);
            setCursor(frame.cursor);
            if (frame.timeframe === timeframe) {
              setXray(frame.xray);
              setLeaders(frame.leaders);
            }
            setStrength((current) =>
              current && current.window === frame.strength?.window ? frame.strength : current
            );
          } else if (frame.type === 'market.error') {
            setNotice(frame.message ?? 'Backend reported a market error.');
          }
        } catch {
          // A malformed frame must not take the dashboard down.
        }
      };
      socket.onclose = () => {
        setWsLive(false);
        schedule();
      };
      socket.onerror = () => socket?.close();
    };

    const schedule = () => {
      if (closed) return;
      retry = Math.min(retry + 1, 6);
      // Backs off to at most 15s so a downed backend is retried without
      // hammering it, and recovers on its own when it returns.
      timer = window.setTimeout(connect, Math.min(1000 * 2 ** (retry - 1), 15000));
    };

    connect();
    return () => {
      closed = true;
      window.clearTimeout(timer);
      socket?.close();
    };
  }, [timeframe]);

  const handleFullscreen = useCallback(() => {
    void toggleFullscreen().then((next) => {
      if (next !== null) setFullscreen(next);
    });
  }, []);

  // F11 is what people press in a desktop window; honour it natively.
  useEffect(() => {
    if (!isDesktop()) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'F11') {
        event.preventDefault();
        handleFullscreen();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [handleFullscreen]);

  const precisionFor = useCallback(
    (sym: string): number =>
      instruments.find((i) => i.symbol === sym)?.price_precision ?? 5,
    [instruments]
  );

  const selectedPrecision = useMemo(
    () => chart?.precision ?? (symbol ? precisionFor(symbol) : 5),
    [chart, symbol, precisionFor]
  );

  if (fatal) {
    return (
      <div className="mx">
        <div className="mx-shell">
          <TopNav status={null} section={section} onSection={setSection} desktop={isDesktop()} />
          <div className="mx-error" style={{ margin: 'auto', maxWidth: 520 }}>
            <strong>{fatal.message}</strong>
            {fatal.remedy && (
              <>
                <br />
                <code>{fatal.remedy}</code>
              </>
            )}
            <br />
            <button
              type="button"
              className="mx-btn"
              style={{ marginTop: 14 }}
              onClick={() => void loadAll(timeframe, symbol, strengthWindow)}
            >
              retry
            </button>
          </div>
        </div>
      </div>
    );
  }

  // The backend clamps the cursor to the store's coverage, so stepping past
  // either end is a no-op rather than an error worth disabling buttons over.
  return (
    <div className="mx">
      <div className="mx-shell">
        <TopNav
          status={status}
          section={section}
          onSection={setSection}
          desktop={isDesktop()}
          fullscreen={fullscreen}
          onToggleFullscreen={handleFullscreen}
        />
        <StatusBanner status={status} />

        {notice && (
          <div className="mx-banner" data-tone="warn">
            <strong>Notice</strong>
            <span>{notice}</span>
            <button
              type="button"
              className="mx-btn"
              style={{ marginLeft: 'auto' }}
              onClick={() => setNotice(null)}
            >
              dismiss
            </button>
          </div>
        )}

        <div className="mx-body">
          {/* ---- left: currency strength ---- */}
          <div className="mx-col">
            <CurrencyStrengthPanel
              snapshot={strength}
              window={strengthWindow}
              windows={STRENGTH_WINDOWS}
              onWindow={handleStrengthWindow}
            />
            <Panel title="Replay" meta={wsLive ? 'ws connected' : 'ws retrying'}>
              <div className="mx-replay">
                <button
                  type="button"
                  className="mx-btn"
                  disabled={stepping}
                  onClick={() => void replay('back', 12)}
                >
                  ⏴⏴ 12
                </button>
                <button
                  type="button"
                  className="mx-btn"
                  disabled={stepping}
                  onClick={() => void replay('back')}
                >
                  ⏴ 1
                </button>
                <button
                  type="button"
                  className="mx-btn"
                  disabled={stepping}
                  onClick={() => void replay('forward')}
                >
                  1 ⏵
                </button>
                <button
                  type="button"
                  className="mx-btn"
                  disabled={stepping}
                  onClick={() => void replay('forward', 12)}
                >
                  12 ⏵⏵
                </button>
                <button
                  type="button"
                  className="mx-btn"
                  disabled={stepping}
                  onClick={() => void replay('reset')}
                >
                  latest
                </button>
              </div>
              <p style={{ marginTop: 9, fontSize: 10.5, color: 'var(--mx-text-faint)' }}>
                Cursor{' '}
                <span className="mx-num" style={{ color: 'var(--mx-text-dim)' }}>
                  {cursor ? `${cursor.slice(0, 10)} ${cursor.slice(11, 16)}` : '—'}
                </span>{' '}
                UTC · steps move by whole {timeframe} bars.
              </p>
            </Panel>
          </div>

          {/* ---- centre: the X-Ray ---- */}
          <div className="mx-col">
            <section className="mx-panel">
              <header className="mx-panel-head">
                <span className="mx-panel-title">Market X-Ray</span>
                <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
                  <span className="mx-panel-meta">
                    {xray ? `${xray.series.length} instruments` : '—'}
                  </span>
                  <Segmented
                    options={TIMEFRAMES}
                    value={timeframe}
                    onChange={handleTimeframe}
                    label="Timeframe"
                  />
                </div>
              </header>
              <div className="mx-panel-body mx-flush">
                <XRayCanvas
                  snapshot={xray}
                  selected={symbol}
                  onSelect={handleSelect}
                  loading={busy}
                />
              </div>
              <div className="mx-chart-legend">
                <span className="mx-swatch">
                  <i style={{ background: 'var(--mx-up)' }} /> rising over window
                </span>
                <span className="mx-swatch">
                  <i style={{ background: 'var(--mx-down)' }} /> falling over window
                </span>
                <span>y = return ÷ own volatility (σ)</span>
                <span style={{ marginLeft: 'auto' }}>
                  hover for detail · click to load the chart
                </span>
              </div>
            </section>

            <section className="mx-panel">
              <header className="mx-panel-head">
                <span className="mx-panel-title">
                  {symbol ?? '—'} · {timeframe}
                </span>
                <span className="mx-panel-meta">
                  {chart?.regime
                    ? `ATR ${chart.regime.atr?.toFixed(selectedPrecision) ?? '—'} · ${chart.regime.label.toLowerCase()}`
                    : '—'}
                  {blocks ? ` · ${blocks.active_count} live zones` : ''}
                </span>
              </header>
              <div className="mx-panel-body mx-flush">
                {chart?.error ? (
                  <div className="mx-empty">{chart.error}</div>
                ) : (
                  <CandleChart
                    candles={chart?.candles ?? []}
                    zones={blocks?.zones ?? []}
                    precision={selectedPrecision}
                    symbol={symbol ?? ''}
                    showInvalidated={showInvalidated}
                  />
                )}
              </div>
              <div className="mx-chart-legend">
                <span className="mx-swatch">
                  <i style={{ background: 'rgba(25, 230, 160, 0.35)' }} /> bullish OB
                </span>
                <span className="mx-swatch">
                  <i style={{ background: 'rgba(255, 77, 126, 0.35)' }} /> bearish OB
                </span>
                <span>solid = live · dashed = invalidated/expired</span>
                {blocks?.nearest?.distance_atr != null && (
                  <span style={{ marginLeft: 'auto' }}>
                    nearest zone {blocks.nearest.distance_atr.toFixed(2)} ATR away
                  </span>
                )}
              </div>
            </section>

            <ZoneTable
              zones={blocks?.zones ?? []}
              precision={selectedPrecision}
              showInvalidated={showInvalidated}
              onToggleInvalidated={() => setShowInvalidated((v) => !v)}
            />

            <EventHistory
              events={events?.events ?? []}
              total={events?.total_stored ?? 0}
              zonesStored={events?.zones_stored ?? 0}
            />
          </div>

          {/* ---- right: leaders ---- */}
          <div className="mx-col">
            <LeaderBoard
              title="Bullish Leaders"
              cards={leaders?.bullish ?? []}
              selected={symbol}
              onSelect={handleSelect}
              precisionFor={precisionFor}
              meta={timeframe}
            />
            <LeaderBoard
              title="Bearish Leaders"
              cards={leaders?.bearish ?? []}
              selected={symbol}
              onSelect={handleSelect}
              precisionFor={precisionFor}
              meta={timeframe}
            />
            <Panel title="Coverage" meta={`${instruments.length} instruments`}>
              <div style={{ fontSize: 10.5, color: 'var(--mx-text-faint)', lineHeight: 1.65 }}>
                <div>
                  Store{' '}
                  <span className="mx-num" style={{ color: 'var(--mx-text-dim)' }}>
                    {instruments[0]?.first_bar?.slice(0, 10) ?? '—'}
                  </span>{' '}
                  →{' '}
                  <span className="mx-num" style={{ color: 'var(--mx-text-dim)' }}>
                    {instruments[0]?.last_bar?.slice(0, 10) ?? '—'}
                  </span>
                </div>
                <div style={{ marginTop: 6 }}>
                  17 FX pairs + XAUUSD are backed by recorded bid/ask data on this machine.
                </div>
                <div style={{ marginTop: 6 }}>
                  Trading execution <strong style={{ color: 'var(--mx-down)' }}>DISABLED</strong>.
                </div>
              </div>
            </Panel>
          </div>
        </div>

        <p className="mx-footnote">
          All values are computed by the TARS backend from recorded bid/ask mid candles. The X-Ray
          measures price-based directional movement, not institutional money flow. Zone rankings are
          descriptive, not win probabilities. No trade is ever placed.
        </p>
      </div>
    </div>
  );
};
