-- Additional exact-stage evidence needed to attribute every physical golden-
-- loop miss to one precise cause: the normalized transcript actually matched
-- against, the specific wake alias recognized (including for two-stage
-- continuations, which previously only recorded the literal string
-- "two_stage_command"), whether a command was extracted from the utterance,
-- the routed intent, when the assistant's response text (pre-TTS) was
-- finalized, when the turn released control back to IDLE, and a free-text
-- failure reason alongside the existing failure stage.

ALTER TABLE voice_turn_traces ADD COLUMN normalized_transcript TEXT;
ALTER TABLE voice_turn_traces ADD COLUMN wake_alias_matched TEXT;
ALTER TABLE voice_turn_traces ADD COLUMN command_extracted INTEGER;
ALTER TABLE voice_turn_traces ADD COLUMN route TEXT;
ALTER TABLE voice_turn_traces ADD COLUMN response_completed_at TEXT;
ALTER TABLE voice_turn_traces ADD COLUMN returned_to_idle_at TEXT;
ALTER TABLE voice_turn_traces ADD COLUMN error_reason TEXT;
