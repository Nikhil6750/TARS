-- 0005_request_traces.sql — per-request latency telemetry (TARS Alexa-Speed
-- Phase A). ProviderDiagnostics (assistant/provider.py) and LatencyTracker
-- (app/latency.py) both already existed but neither was ever persisted --
-- ProviderDiagnostics was computed then discarded in assistant/router.py,
-- and LatencyTracker was only exercised by its own unit tests. This table
-- is what lets a diagnostics endpoint compute real P50/P90/P95 instead of
-- relying on anecdotal single-run numbers in commit messages.

CREATE TABLE IF NOT EXISTS request_traces (
    request_id          TEXT PRIMARY KEY,
    kind                 TEXT NOT NULL,   -- 'chart_analysis' | 'assistant_text'
    conversation_id      TEXT,
    provider_id          TEXT,
    warm_path             INTEGER NOT NULL DEFAULT 0,  -- 1 once Phase D's fast path exists; always 0 today
    started_at           TEXT NOT NULL,
    completed_at          TEXT,
    capture_ms           REAL,   -- client-measured: hide/DWM-wait/BitBlt/restore
    provider_start_ms    REAL,   -- server time from request receipt to provider subprocess dispatch
    first_token_ms        REAL,   -- server time from request receipt to first streamed delta
    provider_latency_ms   REAL,   -- ProviderDiagnostics.latency_ms for non-streaming calls
    total_ms              REAL,
    error                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_request_traces_kind_started ON request_traces(kind, started_at);
