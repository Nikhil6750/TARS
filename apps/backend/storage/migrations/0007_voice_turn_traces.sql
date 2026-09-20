-- Durable physical-voice timing markers. Each timestamp is recorded at the
-- backend boundary where the stage actually occurs; absent stages remain NULL.

CREATE TABLE IF NOT EXISTS voice_turn_traces (
    turn_id                     TEXT PRIMARY KEY,
    conversation_id             TEXT,
    audio_received_at           TEXT,
    stt_started_at              TEXT,
    stt_completed_at            TEXT,
    command_available_at        TEXT,
    assistant_first_text_at     TEXT,
    tts_synthesis_started_at    TEXT,
    tts_ready_at                TEXT,
    status                      TEXT NOT NULL DEFAULT 'in_progress',
    error_stage                 TEXT,
    created_at                  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_voice_turn_conversation_created
ON voice_turn_traces(conversation_id, created_at);
