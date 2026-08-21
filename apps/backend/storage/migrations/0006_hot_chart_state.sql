-- 0006_hot_chart_state.sql — TARS Alexa-Speed Phase B. One row per
-- (chart_window_id, symbol, timeframe) identity, holding the latest
-- vision-grounded chart read for that identity plus enough metadata to
-- mechanically enforce Part 19's cache-correctness rule: a lookup that
-- doesn't match chart_window_id + symbol + timeframe exactly must never
-- reuse a cached row, full stop -- see assistant/hot_chart_state.py's
-- ChartIdentity.matches(). analysis_json is the serialized
-- ChartAnalysisResult (reused, not duplicated into separate columns) so
-- this table doesn't drift from that dataclass's shape over time.
--
-- symbol/timeframe are NOT NULL with '' meaning "unknown" (the Python
-- store layer translates '' <-> None at the boundary) rather than
-- allowing SQL NULL in these columns: SQLite's PRIMARY KEY does not imply
-- NOT NULL for non-INTEGER composite keys, and NULL != NULL for
-- uniqueness purposes, which would silently allow multiple rows for what
-- should be a single "unknown symbol/timeframe" identity on the same
-- window -- exactly the kind of cache-correctness bug Part 19 warns about.

CREATE TABLE IF NOT EXISTS hot_chart_state (
    chart_window_id  TEXT NOT NULL,
    symbol           TEXT NOT NULL DEFAULT '',
    timeframe        TEXT NOT NULL DEFAULT '',
    screenshot_hash  TEXT NOT NULL,
    source           TEXT NOT NULL DEFAULT 'vision',
    observed_at      TEXT NOT NULL,
    analyzed_at      TEXT NOT NULL,
    version          INTEGER NOT NULL DEFAULT 1,
    analysis_json    TEXT NOT NULL,
    PRIMARY KEY (chart_window_id, symbol, timeframe)
);

CREATE INDEX IF NOT EXISTS idx_hot_chart_state_analyzed_at ON hot_chart_state(analyzed_at);
