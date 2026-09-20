-- 0004_skill_invocations.sql — records when TARS actually loads/uses an
-- installed skill's SKILL.md content for a task (Phase 8 of the skill
-- routing pass), distinct from skill_audit (0003), which records
-- install-time quarantine/security validation, not usage. Never stores
-- full conversation content -- `user_task` is the short task description
-- the orchestrator already extracted, not the raw conversation.

CREATE TABLE IF NOT EXISTS skill_invocations (
    invocation_id   TEXT PRIMARY KEY,
    identifier      TEXT NOT NULL,
    content_hash    TEXT,
    invoked_at      TEXT NOT NULL,
    user_task       TEXT NOT NULL DEFAULT '',
    result_status   TEXT NOT NULL DEFAULT 'loaded'  -- 'loaded' | 'failed'
);

CREATE INDEX IF NOT EXISTS idx_skill_invocations_identifier ON skill_invocations(identifier, invoked_at);
