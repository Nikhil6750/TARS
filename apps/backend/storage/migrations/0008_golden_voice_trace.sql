-- Exact-stage evidence for the minimal golden voice loop.  The prior columns
-- remain for compatibility with existing diagnostics; new turns write these
-- canonical names so every physical failure can be assigned to one stage.

ALTER TABLE voice_turn_traces ADD COLUMN audio_detected_at TEXT;
ALTER TABLE voice_turn_traces ADD COLUMN speech_end_at TEXT;
ALTER TABLE voice_turn_traces ADD COLUMN transcript TEXT;
ALTER TABLE voice_turn_traces ADD COLUMN wake_match TEXT;
ALTER TABLE voice_turn_traces ADD COLUMN wake_detected_at TEXT;
ALTER TABLE voice_turn_traces ADD COLUMN command_ready_at TEXT;
ALTER TABLE voice_turn_traces ADD COLUMN processing_started_at TEXT;
ALTER TABLE voice_turn_traces ADD COLUMN first_response_token_at TEXT;
ALTER TABLE voice_turn_traces ADD COLUMN tts_started_at TEXT;
ALTER TABLE voice_turn_traces ADD COLUMN tts_completed_at TEXT;
ALTER TABLE voice_turn_traces ADD COLUMN provider TEXT;
