/**
 * What the Market window shows before there is any market data.
 *
 * A fresh install has an empty candle store. This says what is missing and
 * lets the import run from here, because an installed app has no terminal or
 * checkout to run it from. Nothing is shown as if it were data: with no source
 * files the dashboard stays empty and says why.
 */
import React, { useState } from 'react';
import { api, ApiError } from '../api';
import type { SetupStatus } from '../types';

export const FirstRun: React.FC<{
  setup: SetupStatus;
  onChanged: (next: SetupStatus) => void;
}> = ({ setup, onChanged }) => {
  const [folder, setFolder] = useState(setup.sources.csv_root);
  const [problem, setProblem] = useState<string | null>(null);
  const importing = setup.import.state === 'RUNNING';
  const found = setup.sources.instruments_found.length;
  const total = found + setup.sources.instruments_missing.length;
  const feed = setup.status.feed_connection;

  const check = async () => {
    setProblem(null);
    try {
      onChanged(await api.setup(folder));
    } catch (error) {
      setProblem(error instanceof ApiError ? error.message : String(error));
    }
  };

  const runImport = async () => {
    setProblem(null);
    try {
      await api.startImport(folder);
      onChanged(await api.setup(folder));
    } catch (error) {
      setProblem(error instanceof ApiError ? error.message : String(error));
    }
  };

  return (
    <div className="mx-firstrun">
      <h1>No market history yet</h1>
      <p>
        Market Intelligence computes everything from recorded bid/ask candles or a logged-in
        MetaTrader 5 terminal. This machine has neither yet, so there is nothing to show.
      </p>

      <h2>Import recorded history</h2>
      <p>
        Choose the folder holding your 1-minute bid and ask CSV files. They are read locally and
        are not changed.
      </p>
      <div className="mx-firstrun-row">
        <input
          aria-label="Folder containing bid and ask CSV files"
          value={folder}
          onChange={(event) => setFolder(event.target.value)}
          disabled={importing}
          spellCheck={false}
        />
        <button type="button" className="mx-btn" onClick={() => void check()} disabled={importing}>
          check folder
        </button>
        <button
          type="button"
          className="mx-btn"
          onClick={() => void runImport()}
          disabled={importing || found === 0}
        >
          {importing ? 'importing…' : `import ${found} instrument${found === 1 ? '' : 's'}`}
        </button>
      </div>
      <p className="mx-firstrun-note">
        {setup.sources.csv_root_exists
          ? `${found} of ${total} supported instruments found in this folder.`
          : 'This folder does not exist on this machine.'}
      </p>
      {setup.import.state === 'FAILED' && (
        <p className="mx-firstrun-error">Import failed: {setup.import.error}</p>
      )}
      {problem && <p className="mx-firstrun-error">{problem}</p>}

      <h2>Or connect MetaTrader 5</h2>
      <p>
        {feed === 'AUTH_REQUIRED'
          ? 'A terminal is running but no account is logged in. Log in to it yourself; TARS never asks for or stores broker credentials.'
          : feed === 'CONNECTED'
            ? 'A terminal is connected. Live bars will appear once the market is moving.'
            : 'Start MetaTrader 5 and log in. TARS attaches to it read-only and never places trades.'}
      </p>

      <p className="mx-firstrun-note">
        Candle store: <code>{setup.store_root}</code>
      </p>
    </div>
  );
};
