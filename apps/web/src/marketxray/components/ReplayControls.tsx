/**
 * Replay transport. The cursor lives on the backend, so these only ask it to
 * move: nothing here decides what time it is or what data that moment can see.
 */
import React, { useEffect, useRef, useState } from 'react';
import { Panel } from './Panels';

/** The historical session this build was asked to reconstruct. */
export const REPLAY_WINDOW = {
  label: '27 Oct 2026 · 15:30–19:30 IST',
  symbol: 'XAUUSD',
  timeframe: 'H1',
  start: '2026-10-27T10:00:00+00:00',
  end: '2026-10-27T14:00:00+00:00'
};

const IST_OFFSET_MS = 5.5 * 60 * 60 * 1000;

const istTime = (iso: string): string =>
  new Date(Date.parse(iso) + IST_OFFSET_MS).toISOString().slice(11, 16);

export const ReplayControls: React.FC<{
  cursor: string | null;
  timeframe: string;
  wsLive: boolean;
  stepping: boolean;
  playing: boolean;
  windowCovered: boolean;
  onStep: (bars: number) => void;
  onLatest: () => void;
  onTogglePlay: () => void;
  onSeek: (iso: string) => void;
  onOpenWindow: () => void;
}> = ({
  cursor,
  timeframe,
  wsLive,
  stepping,
  playing,
  windowCovered,
  onStep,
  onLatest,
  onTogglePlay,
  onSeek,
  onOpenWindow
}) => {
  const moving = stepping || playing;
  const [seekTo, setSeekTo] = useState('');
  const seekField = useRef<HTMLInputElement>(null);

  // Follows the cursor, except while someone is typing a time into it.
  useEffect(() => {
    if (cursor && document.activeElement !== seekField.current) setSeekTo(cursor.slice(0, 16));
  }, [cursor]);

  return (
    <Panel title="Replay" meta={wsLive ? 'ws connected' : 'ws retrying'}>
      <div className="mx-replay">
        <button type="button" className="mx-btn" disabled={moving} onClick={() => onStep(-12)}>
          ⏴⏴ 12
        </button>
        <button type="button" className="mx-btn" disabled={moving} onClick={() => onStep(-1)}>
          ⏴ 1
        </button>
        <button
          type="button"
          className="mx-btn"
          disabled={moving}
          aria-label="Next candle"
          title="Next candle"
          onClick={() => onStep(1)}
        >
          1 ⏵
        </button>
        <button type="button" className="mx-btn" disabled={moving} onClick={() => onStep(12)}>
          12 ⏵⏵
        </button>
        <button type="button" className="mx-btn" disabled={moving} onClick={onLatest}>
          latest
        </button>
        <button
          type="button"
          className="mx-btn"
          data-active={playing}
          disabled={stepping && !playing}
          onClick={onTogglePlay}
        >
          {playing ? '⏸ pause' : '▶ play'}
        </button>
      </div>
      <form
        className="mx-replay-seek"
        onSubmit={(event) => {
          event.preventDefault();
          if (seekTo) onSeek(`${seekTo.length === 16 ? `${seekTo}:00` : seekTo}+00:00`);
        }}
      >
        <input
          ref={seekField}
          type="datetime-local"
          aria-label="Seek to UTC time"
          value={seekTo}
          onChange={(event) => setSeekTo(event.target.value)}
        />
        <button type="submit" className="mx-btn" disabled={moving || !seekTo}>
          seek UTC
        </button>
      </form>
      <button
        type="button"
        className="mx-btn mx-replay-window"
        disabled={moving || !windowCovered}
        title={
          windowCovered
            ? `${REPLAY_WINDOW.symbol} ${REPLAY_WINDOW.timeframe}, 10:00–14:00 UTC`
            : 'The active candle store has no data for this date.'
        }
        onClick={onOpenWindow}
      >
        {REPLAY_WINDOW.symbol} · {REPLAY_WINDOW.label}
      </button>
      <p className="mx-replay-note">
        Cursor{' '}
        <span className="mx-num">
          {cursor ? `${cursor.slice(0, 10)} ${cursor.slice(11, 16)}` : '—'}
        </span>{' '}
        UTC{cursor ? ` · ${istTime(cursor)} IST` : ''} · steps move by whole {timeframe} bars.
      </p>
    </Panel>
  );
};
