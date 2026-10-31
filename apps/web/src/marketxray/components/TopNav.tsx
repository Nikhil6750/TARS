/**
 * Top navigation: branding, sections, trading sessions, feed status and the
 * UTC clock.
 *
 * The status chip shows the backend's own label. It is never upgraded on the
 * client: if the backend says REPLAY, this says REPLAY.
 */
import React, { useEffect, useState } from 'react';
import type { MarketStatus, Provenance } from '../types';

export type Section = 'xray' | 'blocks' | 'scanner' | 'journal';

const SECTIONS: Array<{ id: Section; label: string }> = [
  { id: 'xray', label: 'Market X-Ray' },
  { id: 'blocks', label: 'Order Blocks' },
  { id: 'scanner', label: 'Scanner' },
  { id: 'journal', label: 'Journal' }
];

const ALL_SESSIONS = ['SYDNEY', 'TOKYO', 'LONDON', 'NEW_YORK'];

const fmt = (iso: string | null | undefined): string =>
  iso ? `${iso.slice(0, 10)} ${iso.slice(11, 16)}` : '—';

export const TopNav: React.FC<{
  status: MarketStatus | null;
  provenance?: Provenance | null;
  replayPinned?: boolean;
  section: Section;
  onSection: (s: Section) => void;
  desktop?: boolean;
  fullscreen?: boolean;
  onToggleFullscreen?: () => void;
}> = ({ status, provenance, replayPinned, section, onSection, desktop, fullscreen, onToggleFullscreen }) => {
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const active = new Set(status?.sessions_active ?? []);
  const source = provenance?.source ?? (status?.status === 'LIVE' ? 'LIVE_FEED' : 'RECORDED_STORE');
  const sourceLabel = source === 'LIVE_FEED'
    ? `LIVE · ${provenance?.provider ?? 'feed'}`
    : replayPinned
    ? 'REPLAY PINNED'
    : 'RECORDED STORE';

  return (
    <nav className="mx-nav">
      <div className="mx-logo">
        <span className="mx-logo-mark">T</span>
        TARS
      </div>

      <div className="mx-tabs">
        {SECTIONS.map((item) => (
          <button
            key={item.id}
            type="button"
            className="mx-tab"
            aria-current={section === item.id}
            onClick={() => onSection(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div className="mx-nav-right">
        <div className="mx-sessions" title="Approximate liquidity sessions (UTC)">
          {ALL_SESSIONS.map((name) => (
            <span key={name} className="mx-session" data-active={active.has(name)}>
              {name === 'NEW_YORK' ? 'NY' : name.slice(0, 3)}
            </span>
          ))}
        </div>

        <div className="mx-provenance-tag" data-source={source} title={provenance?.detail ?? status?.detail ?? ''}>
          {sourceLabel}
        </div>

        <div className="mx-status" data-status={status?.status ?? 'NO_DATA'} title={status?.detail}>
          <span className="mx-dot" />
          {status?.status ?? 'CONNECTING'}
        </div>

        <div className="mx-clock mx-num">
          {now.toISOString().slice(11, 19)} UTC
          <span>data {fmt(status?.data_as_of)}</span>
        </div>

        {desktop && onToggleFullscreen && (
          <button
            type="button"
            className="mx-btn"
            onClick={onToggleFullscreen}
            title={fullscreen ? 'Leave fullscreen (F11)' : 'Fullscreen (F11)'}
            aria-pressed={fullscreen}
          >
            {fullscreen ? '⤢ exit' : '⤢ full'}
          </button>
        )}
      </div>
    </nav>
  );
};

export const StatusBanner: React.FC<{
  status: MarketStatus | null;
  provenance?: Provenance | null;
  replayPinned?: boolean;
}> = ({ status, provenance, replayPinned }) => {
  if (!status) return null;
  const tone =
    status.status === 'CLOSED' ? 'closed' : status.status === 'LIVE' ? 'live' : 'warn';
  const source = provenance?.source ?? (status.status === 'LIVE' ? 'LIVE_FEED' : 'RECORDED_STORE');
  const sourceLabel = source === 'LIVE_FEED'
    ? `LIVE FEED (${provenance?.provider ?? 'provider'})`
    : 'RECORDED STORE (HISTORICAL)';

  return (
    <div className="mx-banner" data-tone={tone}>
      <span className="mx-provenance-badge" data-source={source}>
        {sourceLabel}
      </span>
      {replayPinned && (
        <span className="mx-pinned-badge" title="Replay cursor is pinned to historical bar">
          REPLAY PINNED
        </span>
      )}
      <strong>{status.market_open ? 'MARKET OPEN' : 'MARKET CLOSED'}</strong>
      <span>{status.detail}</span>
      {status.next_transition && (
        <span style={{ marginLeft: 'auto', fontFamily: 'var(--mx-mono)', fontSize: '10.5px' }}>
          next {status.market_open ? 'close' : 'open'} {fmt(status.next_transition)} UTC
        </span>
      )}
    </div>
  );
};
