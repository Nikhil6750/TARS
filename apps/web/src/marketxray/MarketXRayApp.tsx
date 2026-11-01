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
import { backendStatus, isDesktop, loadPreferences, savePreferences, toggleFullscreen } from './desktop';
import { FirstRun } from './components/FirstRun';
import { REPLAY_WINDOW, ReplayControls } from './components/ReplayControls';
import { XRayCanvas } from './components/XRayCanvas';
import type {
  ChartPayload,
  Dashboard,
  EventsPayload,
  Instrument,
  LeadersSnapshot,
  MarketStatus,
  OrderBlocksPayload,
  Provenance,
  SetupStatus,
  StrengthSnapshot,
  XRaySnapshot
} from './types';

const TIMEFRAMES = ['M5', 'M15', 'H1', 'H4', 'D1'];
const STRENGTH_WINDOWS = ['1H', '4H', '1D'];
export const PLAY_STEP_MS = 1500;

// Preferences can outlive a change of candle store. Let the backend choose an
// available instrument when that saved symbol no longer exists in this store.
async function dashboardForStore(tf: string, sym: string | null, win: string): Promise<Dashboard> {
  try {
    return await api.dashboard(tf, sym ?? undefined, win);
  } catch (error) {
    if (!sym || !(error instanceof ApiError) || error.status !== 404) throw error;
    return api.dashboard(tf, undefined, win);
  }
}

const isInReplayWindow = (moment: string | null): boolean => {
  if (!moment) return false;
  const at = Date.parse(moment);
  return at >= Date.parse(REPLAY_WINDOW.start) && at < Date.parse(REPLAY_WINDOW.end);
};

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
  const [coverage, setCoverage] = useState<{ first: string | null; last: string | null } | null>(null);
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [xray, setXray] = useState<XRaySnapshot | null>(null);
  const [strength, setStrength] = useState<StrengthSnapshot | null>(null);
  const [leaders, setLeaders] = useState<LeadersSnapshot | null>(null);
  const [chart, setChart] = useState<ChartPayload | null>(null);
  const [blocks, setBlocks] = useState<OrderBlocksPayload | null>(null);
  const [events, setEvents] = useState<EventsPayload | null>(null);
  const [provenance, setProvenance] = useState<Provenance | null>(null);
  const [replayPinned, setReplayPinned] = useState(false);
  const [setup, setSetup] = useState<SetupStatus | null>(null);

  const [fatal, setFatal] = useState<Fatal | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [busy, setBusy] = useState(true);
  const [wsLive, setWsLive] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  // Guards against a slow earlier response overwriting a newer one.
  const requestSeq = useRef(0);

  const applyDashboard = useCallback((data: Dashboard) => {
    setStatus(data.status);
    setCursor(data.cursor.as_of);
    setCoverage({ first: data.cursor.first_bar, last: data.cursor.last_bar });
    setInstruments(data.instruments);
    setXray(data.xray);
    setStrength(data.strength);
    setLeaders(data.leaders);
    setChart(data.chart);
    setBlocks(data.order_blocks);
    setEvents(data.events);
    setProvenance(data.provenance ?? null);
    setReplayPinned(data.replay_pinned ?? false);
    setSymbol((current) =>
      data.instruments.some((instrument) => instrument.symbol === current)
        ? current
        : data.selected_symbol
    );
    setTimeframe(data.timeframe);
  }, []);

  const loadAll = useCallback(
    async (tf: string, sym: string | null, win: string) => {
      const seq = ++requestSeq.current;
      setBusy(true);
      try {
        const state = await api.setup();
        if (seq !== requestSeq.current) return;
        if (!state.ready) {
          setSetup(state);
          setFatal(null);
          return;
        }
        const data = await dashboardForStore(tf, sym, win);
        if (seq !== requestSeq.current) return;
        applyDashboard(data);
        setSetup(null);
        setFatal(null);
      } catch (error) {
        if (seq !== requestSeq.current) return;
        if (error instanceof ApiError) {
          // The shell knows why, when something else is holding the port.
          const shell = error.status === 0 ? await backendStatus() : null;
          setFatal({
            message: error.message,
            remedy: shell?.ownership === 'CONFLICT' ? `Port conflict: ${shell.detail}` : error.remedy
          });
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

  // Retry automatically while the backend is unreachable.
  //
  // This matters most on a cold desktop start: the Tauri shell opens this
  // window and starts the Python backend at the same time, so the first
  // fetch regularly lands before the backend is accepting connections.
  // Without this the window would sit on a dead-end error panel until
  // someone pressed retry, even though the backend came up a second later.
  useEffect(() => {
    if (!fatal) {
      setRetrying(false);
      return;
    }
    let cancelled = false;
    let attempt = 0;
    let timer = 0;

    const tick = () => {
      if (cancelled) return;
      attempt += 1;
      setRetrying(true);
      void api
        .setup()
        .then(async (state) => {
          if (cancelled) return;
          if (!state.ready) {
            setSetup(state);
          } else {
            applyDashboard(await dashboardForStore(timeframe, symbol, strengthWindow));
          }
          if (cancelled) return;
          setFatal(null);
          setRetrying(false);
        })
        .catch(() => {
          if (cancelled) return;
          // Back off to 8s so a genuinely absent backend is not hammered,
          // while a backend that is merely still starting is picked up fast.
          timer = window.setTimeout(tick, Math.min(1000 * 2 ** (attempt - 1), 8000));
        });
    };

    timer = window.setTimeout(tick, 1200);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [fatal, timeframe, symbol, strengthWindow, applyDashboard]);

  // While there is nothing to show, watch for that changing: an import
  // finishing, or a logged-in terminal delivering its first bars.
  useEffect(() => {
    if (!setup) return;
    const timer = window.setInterval(() => {
      void api
        .setup(setup.sources.csv_root)
        .then((state) => {
          if (state.ready) void loadAll(timeframe, symbol, strengthWindow);
          else setSetup(state);
        })
        .catch(() => undefined);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [setup, loadAll, timeframe, symbol, strengthWindow]);

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
  const [playing, setPlaying] = useState(false);

  /** Move the backend's cursor, then redraw everything at the new moment. */
  const moveCursor = useCallback(
    async (move: () => Promise<unknown>, tf: string, sym: string | null) => {
      setStepping(true);
      try {
        await move();
        await loadAll(tf, sym, strengthWindow);
      } catch (error) {
        setPlaying(false);
        setNotice(error instanceof ApiError ? error.message : 'Replay step failed.');
      } finally {
        setStepping(false);
      }
    },
    [strengthWindow, loadAll]
  );

  const handleStep = useCallback(
    (bars: number) => void moveCursor(() => api.step(timeframe, bars), timeframe, symbol),
    [moveCursor, timeframe, symbol]
  );

  const handleLatest = useCallback(
    () => void moveCursor(() => api.reset(), timeframe, symbol),
    [moveCursor, timeframe, symbol]
  );

  const handleSeek = useCallback(
    (iso: string) =>
      void moveCursor(
        async () => {
          const moved = await api.seek(iso);
          // The backend clamps to what the store covers, which is worth saying.
          setNotice(
            Date.parse(moved.cursor) === Date.parse(iso)
              ? null
              : `The store has no data at that moment, so the cursor moved to ${moved.cursor
                  .slice(0, 16)
                  .replace('T', ' ')} UTC.`
          );
        },
        timeframe,
        symbol
      ),
    [moveCursor, timeframe, symbol]
  );

  const handleOpenWindow = useCallback(() => {
    setSymbol(REPLAY_WINDOW.symbol);
    setTimeframe(REPLAY_WINDOW.timeframe);
    void moveCursor(() => api.seek(REPLAY_WINDOW.start), REPLAY_WINDOW.timeframe, REPLAY_WINDOW.symbol);
  }, [moveCursor]);

  const cursorRef = useRef(cursor);
  cursorRef.current = cursor;

  // Play is the next-candle step on a timer. Each waits for the last to finish
  // drawing, so a slow machine plays slower rather than queueing steps up.
  useEffect(() => {
    if (!playing) return;
    let cancelled = false;
    let timer = 0;
    const tick = async () => {
      const before = cursorRef.current;
      try {
        const moved = await api.step(timeframe, 1);
        if (cancelled) return;
        await loadAll(timeframe, symbol, strengthWindow);
        if (cancelled) return;
        const leftWindow = isInReplayWindow(before) && !isInReplayWindow(moved.cursor);
        if (moved.cursor === before || leftWindow) {
          setPlaying(false);
          return;
        }
        timer = window.setTimeout(() => void tick(), PLAY_STEP_MS);
      } catch (error) {
        if (cancelled) return;
        setPlaying(false);
        setNotice(error instanceof ApiError ? error.message : 'Replay step failed.');
      }
    };
    timer = window.setTimeout(() => void tick(), PLAY_STEP_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [playing, timeframe, symbol, strengthWindow, loadAll]);

  const handleSection = useCallback((next: Section) => {
    setSection(next);
    document.getElementById(`mx-section-${next}`)?.scrollIntoView?.({ behavior: 'smooth' });
  }, []);

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
            if (frame.provenance) {
              setProvenance(frame.provenance);
            }
            if (typeof frame.replay_pinned === 'boolean') {
              setReplayPinned(frame.replay_pinned);
            }
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
      if (event.key === 'F11' || event.code === 'F11' || event.keyCode === 122) {
        event.preventDefault();
        event.stopPropagation();
        handleFullscreen();
      }
    };
    window.addEventListener('keydown', onKey, true);
    document.addEventListener('keydown', onKey, true);
    return () => {
      window.removeEventListener('keydown', onKey, true);
      document.removeEventListener('keydown', onKey, true);
    };
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

  const windowCovered = Boolean(
    coverage?.first &&
      coverage.last &&
      Date.parse(coverage.first) <= Date.parse(REPLAY_WINDOW.start) &&
      Date.parse(coverage.last) >= Date.parse(REPLAY_WINDOW.end)
  );

  if (setup) {
    return (
      <div className="mx">
        <div className="mx-shell">
          <TopNav status={setup.status} section={section} onSection={setSection} desktop={isDesktop()} />
          <FirstRun setup={setup} onChanged={setSetup} />
        </div>
      </div>
    );
  }

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
            <span
              style={{
                display: 'block',
                marginTop: 10,
                color: 'var(--mx-text-faint)',
                fontSize: 11
              }}
            >
              {retrying
                ? 'Retrying — if TARS has just started, its backend is probably still coming up.'
                : 'Will keep retrying automatically.'}
            </span>
            <button
              type="button"
              className="mx-btn"
              style={{ marginTop: 14 }}
              onClick={() => void loadAll(timeframe, symbol, strengthWindow)}
            >
              retry now
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
          provenance={provenance}
          replayPinned={replayPinned}
          section={section}
          onSection={handleSection}
          desktop={isDesktop()}
          fullscreen={fullscreen}
          onToggleFullscreen={handleFullscreen}
        />
        <StatusBanner status={status} provenance={provenance} replayPinned={replayPinned} />

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
            <ReplayControls
              cursor={cursor}
              timeframe={timeframe}
              wsLive={wsLive}
              stepping={stepping}
              playing={playing}
              windowCovered={windowCovered}
              onStep={handleStep}
              onLatest={handleLatest}
              onTogglePlay={() => setPlaying((value) => !value)}
              onSeek={handleSeek}
              onOpenWindow={handleOpenWindow}
            />
          </div>

          {/* ---- centre: the X-Ray ---- */}
          <div className="mx-col">
            <section className="mx-panel mx-anchor" id="mx-section-xray">
              <header className="mx-panel-head">
                <span className="mx-panel-title">Market X-Ray</span>
                <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
                  <span className="mx-panel-meta">
                    {xray ? `${xray.series.length} instrument${xray.series.length === 1 ? '' : 's'}` : '—'}
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

            <div className="mx-anchor" id="mx-section-blocks">
              <ZoneTable
                zones={blocks?.zones ?? []}
                precision={selectedPrecision}
                showInvalidated={showInvalidated}
                onToggleInvalidated={() => setShowInvalidated((v) => !v)}
              />
            </div>

            <div className="mx-anchor" id="mx-section-journal">
              <EventHistory
                events={events?.events ?? []}
                total={events?.total_stored ?? 0}
                zonesStored={events?.zones_stored ?? 0}
              />
            </div>
          </div>

          {/* ---- right: leaders ---- */}
          <div className="mx-col mx-anchor" id="mx-section-scanner">
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
            <Panel
              title="Coverage"
              meta={`${instruments.length} instrument${instruments.length === 1 ? '' : 's'}`}
            >
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
                  {instruments.length === 1 ? (
                    <>
                      Only {instruments[0].symbol} is in this store. Cross-instrument rankings are
                      unavailable.
                    </>
                  ) : (
                    <>
                      {instruments.filter((i) => i.asset_class === 'FX').length} FX pairs + XAUUSD
                      are backed by recorded bid/ask data on this machine.
                    </>
                  )}
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
