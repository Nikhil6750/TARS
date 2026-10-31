/**
 * Side and bottom panels: currency strength, leader boards, zone tables and
 * event history.
 *
 * These render backend values verbatim. Where a number is weakly supported --
 * a currency score built from one or two pairs -- the panel says so on the row
 * rather than presenting it with the same authority as a well-covered one.
 */
import React from 'react';
import type {
  CurrencyStrength,
  LeaderCard,
  OrderBlock,
  StoredEvent,
  StrengthSnapshot
} from '../types';

export const Panel: React.FC<{
  title: string;
  meta?: React.ReactNode;
  children: React.ReactNode;
  flush?: boolean;
  actions?: React.ReactNode;
}> = ({ title, meta, children, flush, actions }) => (
  <section className="mx-panel">
    <header className="mx-panel-head">
      <span className="mx-panel-title">{title}</span>
      {actions ?? (meta ? <span className="mx-panel-meta">{meta}</span> : null)}
    </header>
    <div className={`mx-panel-body${flush ? ' mx-flush' : ''}`}>{children}</div>
  </section>
);

export const Segmented: React.FC<{
  options: string[];
  value: string;
  onChange: (next: string) => void;
  label?: string;
}> = ({ options, value, onChange, label }) => (
  <div className="mx-seg" role="group" aria-label={label}>
    {options.map((option) => (
      <button
        key={option}
        type="button"
        aria-pressed={value === option}
        onClick={() => onChange(option)}
      >
        {option}
      </button>
    ))}
  </div>
);

const signed = (value: number, digits = 2): string =>
  `${value >= 0 ? '+' : ''}${value.toFixed(digits)}`;

/* ---------- currency strength ---------- */
export const CurrencyStrengthPanel: React.FC<{
  snapshot: StrengthSnapshot | null;
  window: string;
  windows: string[];
  onWindow: (w: string) => void;
}> = ({ snapshot, window: win, windows, onWindow }) => {
  const peak = snapshot
    ? Math.max(0.6, ...snapshot.ranking.map((r) => Math.abs(r.score)))
    : 1;
  const thin = snapshot?.ranking.filter((r) => r.thin_coverage) ?? [];

  return (
    <Panel
      title="Currency Strength"
      flush
      actions={<Segmented options={windows} value={win} onChange={onWindow} label="Strength window" />}
    >
      {!snapshot ? (
        <div className="mx-panel-body">
          {[0, 1, 2, 3, 4, 5, 6, 7].map((i) => (
            <div key={i} className="mx-skeleton" style={{ margin: '9px 0' }} />
          ))}
        </div>
      ) : (
        <>
          <div className="mx-ccy">
            {snapshot.ranking.map((row: CurrencyStrength) => {
              const up = row.score >= 0;
              const width = (Math.abs(row.score) / peak) * 50;
              return (
                <div
                  className="mx-ccy-row"
                  key={row.currency}
                  title={
                    `${row.currency}: ${signed(row.score, 3)} from ${row.pairs_used} pair(s) ` +
                    `(${row.counterparties.join(', ')}).\n` +
                    `Leave-one-pair-out max shift ${row.leave_one_out_max_delta.toFixed(3)}` +
                    (row.leave_one_out_driver ? ` (driven by ${row.leave_one_out_driver})` : '') +
                    (row.thin_coverage ? '\nThin coverage: too few pairs to cross-check.' : '')
                  }
                >
                  <span className="mx-ccy-code">
                    {row.currency}
                    {row.thin_coverage && <span className="mx-thin">{row.pairs_used}P</span>}
                  </span>
                  <span className="mx-ccy-track">
                    <span
                      className="mx-ccy-bar"
                      data-dir={up ? 'up' : 'down'}
                      style={{
                        left: up ? '50%' : `${50 - width}%`,
                        width: `${Math.max(width, 0.6)}%`
                      }}
                    />
                  </span>
                  <span className={`mx-ccy-score ${up ? 'mx-up' : 'mx-down'}`}>
                    {signed(row.score)}
                  </span>
                </div>
              );
            })}
          </div>
          <p className="mx-coverage-note">
            {snapshot.pairs_evaluated.length} pairs · {snapshot.timeframe} × {snapshot.bars} bars.
            {thin.length > 0 && (
              <>
                {' '}
                <strong style={{ color: 'var(--mx-warn)' }}>
                  {thin.map((t) => `${t.currency} (${t.pairs_used}p)`).join(', ')}
                </strong>{' '}
                rest on too few pairs to cross-check.
              </>
            )}{' '}
            Relative price movement, not flow.
          </p>
        </>
      )}
    </Panel>
  );
};

/* ---------- leader boards ---------- */
const LeaderRow: React.FC<{
  card: LeaderCard;
  selected: boolean;
  onSelect: (symbol: string) => void;
  precision: number;
}> = ({ card, selected, onSelect, precision }) => {
  const up = card.pct_change >= 0;
  const zone = card.nearest_zone;
  return (
    <button
      type="button"
      className="mx-card"
      data-selected={selected}
      onClick={() => onSelect(card.symbol)}
    >
      <span className="mx-card-sym">
        {card.symbol}
        <span className="mx-card-rank">#{card.rank}</span>
      </span>
      <span className="mx-card-price mx-num">{card.close.toFixed(precision)}</span>
      <span className="mx-card-sub">
        <span>{signed(card.z)}σ</span>
        <span>{card.regime.toLowerCase()}</span>
        {zone?.distance_atr != null && (
          <span title={`Nearest ${zone.side} order block, ${zone.status}`}>
            OB {zone.distance_atr.toFixed(1)} ATR
          </span>
        )}
      </span>
      <span className={`mx-card-pct ${up ? 'mx-up' : 'mx-down'}`}>
        {signed(card.pct_change, 3)}%
      </span>
    </button>
  );
};

export const LeaderBoard: React.FC<{
  title: string;
  cards: LeaderCard[];
  selected: string | null;
  onSelect: (symbol: string) => void;
  precisionFor: (symbol: string) => number;
  meta?: string;
}> = ({ title, cards, selected, onSelect, precisionFor, meta }) => (
  <Panel title={title} meta={meta} flush>
    {cards.length === 0 ? (
      <div className="mx-empty">No instruments on this side at this moment.</div>
    ) : (
      <div className="mx-cards">
        {cards.map((card) => (
          <LeaderRow
            key={card.symbol}
            card={card}
            selected={selected === card.symbol}
            onSelect={onSelect}
            precision={precisionFor(card.symbol)}
          />
        ))}
      </div>
    )}
  </Panel>
);

/* ---------- zone table ---------- */
export const ZoneTable: React.FC<{
  zones: OrderBlock[];
  precision: number;
  showInvalidated: boolean;
  onToggleInvalidated: () => void;
}> = ({ zones, precision, showInvalidated, onToggleInvalidated }) => {
  const live = new Set(['FRESH', 'TAPPED', 'MITIGATED']);
  const rows = zones
    .filter((z) => showInvalidated || live.has(z.status))
    .sort((a, b) => {
      const da = a.distance_atr ?? 1e9;
      const db = b.distance_atr ?? 1e9;
      return da - db;
    });

  return (
    <Panel
      title="Order Blocks"
      actions={
        <button
          type="button"
          className="mx-btn"
          data-active={showInvalidated}
          onClick={onToggleInvalidated}
        >
          {showInvalidated ? 'all zones' : 'live only'}
        </button>
      }
      flush
    >
      {rows.length === 0 ? (
        <div className="mx-empty">
          No qualifying zones in the scanned window.
          <br />
          Detection requires a displacement candle, an origin candle and a break of structure.
        </div>
      ) : (
        <div className="mx-scroll">
          <table className="mx-table">
            <thead>
              <tr>
                <th>Side</th>
                <th>Zone</th>
                <th>State</th>
                <th>Dist</th>
                <th>Disp</th>
                <th>Age</th>
                <th>Rank</th>
                <th>Formed (UTC)</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((z) => (
                <tr key={z.zone_id} title={z.score_basis}>
                  <td className={z.side === 'BULLISH' ? 'mx-side-up' : 'mx-side-down'}>
                    {z.side === 'BULLISH' ? 'BULL' : 'BEAR'}
                  </td>
                  <td>
                    {z.zone_low.toFixed(precision)} – {z.zone_high.toFixed(precision)}
                  </td>
                  <td>
                    <span className="mx-pill" data-s={z.status}>
                      {z.status}
                    </span>
                  </td>
                  <td>{z.distance_atr != null ? `${z.distance_atr.toFixed(2)} ATR` : '—'}</td>
                  <td>{z.displacement_atr.toFixed(2)}×</td>
                  <td>{z.age_bars}b</td>
                  <td>{z.score.toFixed(0)}</td>
                  <td>{z.origin_ts.slice(0, 16).replace('T', ' ')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
};

/* ---------- event history ---------- */
export const EventHistory: React.FC<{
  events: StoredEvent[];
  total: number;
  zonesStored: number;
}> = ({ events, total, zonesStored }) => (
  <Panel
    title="Event History"
    meta={`${total} stored · ${zonesStored} zones`}
    flush
  >
    {events.length === 0 ? (
      <div className="mx-empty">No zone interactions recorded yet.</div>
    ) : (
      <div className="mx-scroll">
        <table className="mx-table">
          <thead>
            <tr>
              <th>Time (UTC)</th>
              <th>Symbol</th>
              <th>TF</th>
              <th>Event</th>
              <th>Price</th>
            </tr>
          </thead>
          <tbody>
            {events.map((event) => (
              <tr key={event.event_id}>
                <td>{event.ts.slice(0, 16).replace('T', ' ')}</td>
                <td>{event.symbol}</td>
                <td>{event.timeframe}</td>
                <td>
                  <span className="mx-pill" data-s={event.kind}>
                    {event.kind}
                  </span>
                </td>
                <td>{event.price.toPrecision(7)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )}
  </Panel>
);
