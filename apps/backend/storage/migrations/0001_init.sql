-- 0001_init.sql — operational state + conversation memory schema.
-- Applied in order by app/db.py's migration runner, tracked in schema_migrations.

CREATE TABLE IF NOT EXISTS trading_events (
    event_id            TEXT PRIMARY KEY,
    schema_version      TEXT NOT NULL,
    timestamp           TEXT NOT NULL,
    source              TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    strategy_id         TEXT,
    state               TEXT NOT NULL,
    direction           TEXT,
    entry               REAL,
    stop_loss           REAL,
    take_profit         REAL,
    risk_reward         REAL,
    risk_percent        REAL,
    validation_status   TEXT NOT NULL,
    reason_codes        TEXT NOT NULL DEFAULT '[]',
    warnings            TEXT NOT NULL DEFAULT '[]',
    expires_at          TEXT,
    received_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_trading_events_symbol ON trading_events(symbol);
CREATE INDEX IF NOT EXISTS idx_trading_events_state ON trading_events(state);
CREATE INDEX IF NOT EXISTS idx_trading_events_received_at ON trading_events(received_at);

-- One row per symbol: the current "active setup" derived deterministically
-- from the trading_events history. Never written to directly by clients.
CREATE TABLE IF NOT EXISTS active_setups (
    symbol              TEXT PRIMARY KEY,
    event_id            TEXT NOT NULL REFERENCES trading_events(event_id),
    state               TEXT NOT NULL,
    validation_status   TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_messages (
    message_id          TEXT PRIMARY KEY,
    schema_version      TEXT NOT NULL,
    conversation_id     TEXT NOT NULL,
    timestamp            TEXT NOT NULL,
    role                 TEXT NOT NULL,
    content              TEXT NOT NULL,
    input_mode           TEXT NOT NULL,
    audio_ref            TEXT,
    related_event_id     TEXT,
    intent                TEXT,
    provider_stt          TEXT,
    provider_assistant    TEXT,
    provider_tts           TEXT,
    error                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_conversation_messages_conversation
    ON conversation_messages(conversation_id, timestamp);

-- Full-text search over conversation memory + indexed vault notes.
-- `source` distinguishes 'conversation' rows from 'vault' rows; `source_id`
-- is the conversation message_id or vault-relative file path respectively.
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    source UNINDEXED,
    source_id UNINDEXED,
    title,
    body,
    tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS vault_documents (
    path                 TEXT PRIMARY KEY,
    title                 TEXT NOT NULL,
    content_hash          TEXT NOT NULL,
    indexed_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id            TEXT PRIMARY KEY,
    conversation_id        TEXT NOT NULL,
    started_at              TEXT NOT NULL,
    last_active_at            TEXT NOT NULL,
    client                   TEXT
);
