-- Semantic facts extend the existing provenance-bearing notes, in the same DB.
-- metadata contains the canonical fact; body retains the user's local evidence.
ALTER TABLE memory_notes ADD COLUMN semantic_key TEXT;
ALTER TABLE memory_notes ADD COLUMN supersedes TEXT;
ALTER TABLE memory_notes ADD COLUMN superseded_by TEXT;

CREATE UNIQUE INDEX idx_memory_semantic_active ON memory_notes(semantic_key)
    WHERE semantic_key IS NOT NULL AND superseded_by IS NULL;
CREATE INDEX idx_memory_semantic_lookup ON memory_notes(
    json_extract(metadata, '$.scope'), json_extract(metadata, '$.subject'),
    json_extract(metadata, '$.relation'))
    WHERE semantic_key IS NOT NULL AND superseded_by IS NULL;

-- Compare-and-swap plus retirement happens within the INSERT statement, so
-- cancellation, concurrent services, or other users of the shared connection
-- cannot leave a retired fact without its replacement.
CREATE TRIGGER memory_semantic_replace BEFORE INSERT ON memory_notes
WHEN NEW.semantic_key IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'semantic memory conflict') WHERE
        (NEW.supersedes IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM memory_notes WHERE note_id = NEW.supersedes
            AND semantic_key = NEW.semantic_key AND superseded_by IS NULL))
        OR EXISTS (SELECT 1 FROM memory_notes WHERE semantic_key = NEW.semantic_key
            AND superseded_by IS NULL AND note_id IS NOT NEW.supersedes);
    UPDATE memory_notes SET superseded_by = NEW.note_id,
        metadata = json_set(metadata, '$.updated_at', NEW.created_at)
        WHERE note_id = NEW.supersedes;
    UPDATE memory_notes SET superseded_by = NEW.note_id
        WHERE note_id = json_extract(NEW.metadata, '$.source.legacy_note_id')
        AND semantic_key IS NULL AND actor = 'user' AND kind = 'explicit_memory';
    DELETE FROM memory_fts WHERE source_id = NEW.supersedes AND source = 'explicit_memory';
    DELETE FROM memory_fts WHERE source_id = json_extract(NEW.metadata, '$.source.legacy_note_id')
        AND source = 'explicit_memory';
END;

CREATE TRIGGER memory_semantic_index AFTER INSERT ON memory_notes
WHEN NEW.semantic_key IS NOT NULL
BEGIN
    INSERT INTO memory_fts(source, source_id, title, body) VALUES (
        'explicit_memory', NEW.note_id, 'Semantic memory',
        json_extract(NEW.metadata, '$.subject') || ' ' ||
        json_extract(NEW.metadata, '$.relation') || ' ' ||
        json_extract(NEW.metadata, '$.object'));
END;

CREATE TRIGGER memory_semantic_delete AFTER DELETE ON memory_notes
WHEN OLD.semantic_key IS NOT NULL
BEGIN
    DELETE FROM memory_fts WHERE source = 'explicit_memory' AND source_id = OLD.note_id;
END;
