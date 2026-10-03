-- Generic small persistent key-value app state (e.g. last_daily_brief_date)
-- that is NOT per-conversation memory and NOT env-sourced Settings -- small
-- runtime facts that must survive a backend restart. Deliberately a plain
-- key/value table, not a one-off column bolted onto an unrelated table.

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
