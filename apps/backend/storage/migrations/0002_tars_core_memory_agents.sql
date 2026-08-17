-- 0002_tars_core_memory_agents.sql — structured memory notes (explicit
-- "remember this", trading observations, task/decision records) and agent
-- run audit trail, for the TARS Orchestrator / Trading Intelligence /
-- Agent framework work. `memory_fts` (0001) already indexes title/body
-- text for any source; this migration adds the structured/provenance side
-- that free-text search alone can't answer (list by kind, by symbol, by
-- conversation, delete-by-id for "forget that").

-- One row per structured memory note. `source_id` matches the `source_id`
-- used in the corresponding `memory_fts` row (same source string too), so
-- an FTS search hit can be resolved back to its full structured record.
CREATE TABLE IF NOT EXISTS memory_notes (
    note_id         TEXT PRIMARY KEY,
    kind            TEXT NOT NULL, -- 'explicit_memory' | 'trading_observation' | 'decision'
    source_id       TEXT NOT NULL, -- same id used as memory_fts.source_id
    actor           TEXT NOT NULL, -- 'user' | 'agent:<agent_name>' | 'system'
    conversation_id TEXT,
    symbol          TEXT,
    tags            TEXT NOT NULL DEFAULT '[]',
    body            TEXT NOT NULL,
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_notes_kind ON memory_notes(kind, created_at);
CREATE INDEX IF NOT EXISTS idx_memory_notes_symbol ON memory_notes(symbol);
CREATE INDEX IF NOT EXISTS idx_memory_notes_conversation ON memory_notes(conversation_id);

-- Agent framework audit trail: one row per agent run (ON_DEMAND invocation,
-- one SCHEDULED firing, or one CONTINUOUS iteration), mirroring the
-- append-only audit style already used for actions/plans.
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id          TEXT PRIMARY KEY,
    agent_name      TEXT NOT NULL,
    mode            TEXT NOT NULL, -- 'ON_DEMAND' | 'SCHEDULED' | 'CONTINUOUS'
    status          TEXT NOT NULL, -- 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'TIMED_OUT' | 'CANCELLED'
    trigger         TEXT NOT NULL, -- what started this run, e.g. 'api', 'scheduler', 'continuous_loop'
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    iterations      INTEGER NOT NULL DEFAULT 0,
    summary         TEXT,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_agent ON agent_runs(agent_name, started_at);
