-- 0003_skill_registry.sql — Hermes-aggregated skill CATALOG (metadata for
-- every discoverable skill) plus TARS's own INSTALLED skill bundles. These
-- are deliberately separate: skill_catalog holds ~90k lightweight metadata
-- rows for local search (never loaded into a Claude prompt), while
-- installed_skills tracks the handful of full SKILL.md bundles TARS has
-- actually chosen to install into the Obsidian vault (see
-- skill_registry/manager.py). Distinct from the existing `skills` package
-- (apps/backend/skills/) — TARS's own ActionRuntime action handlers
-- (filesystem, browser, trading, ...) — which this migration does not
-- touch.

CREATE TABLE IF NOT EXISTS skill_catalog (
    identifier      TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL,
    trust_level     TEXT NOT NULL DEFAULT 'community',
    repo            TEXT,
    path            TEXT,
    tags            TEXT NOT NULL DEFAULT '[]',   -- JSON array
    platform        TEXT NOT NULL DEFAULT '[]',   -- JSON array, e.g. ["windows","macos","linux"]; [] = unknown/unspecified
    extra           TEXT NOT NULL DEFAULT '{}',   -- JSON object, upstream metadata passthrough
    content_hash    TEXT NOT NULL,                -- sha256 of this record's normalized JSON, for change detection
    catalog_version INTEGER NOT NULL,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_skill_catalog_source ON skill_catalog(source);
CREATE INDEX IF NOT EXISTS idx_skill_catalog_trust ON skill_catalog(trust_level);

-- FTS5 index over the searchable fields (Phase 7). Mirrors memory_fts's
-- tokenizer choice (0001_init.sql) for consistency; content is duplicated
-- here (not a content= external-content table) to keep search independent
-- of skill_catalog's exact column set.
CREATE VIRTUAL TABLE IF NOT EXISTS skill_catalog_fts USING fts5(
    identifier UNINDEXED,
    name,
    description,
    tags,
    source,
    tokenize = 'porter unicode61'
);

-- One row per upstream source the catalog aggregates (skills.sh, github,
-- clawhub, lobehub, browse-sh, official, claude-marketplace, ...).
CREATE TABLE IF NOT EXISTS skill_sources (
    source          TEXT PRIMARY KEY,
    record_count    INTEGER NOT NULL DEFAULT 0,
    last_synced_at  TEXT,
    notes           TEXT
);

-- Append-only log of catalog sync attempts, per Phase 15's reporting
-- requirements (no fabricated numbers -- always read this table back).
CREATE TABLE IF NOT EXISTS skill_sync_log (
    sync_id                 TEXT PRIMARY KEY,
    started_at              TEXT NOT NULL,
    finished_at             TEXT,
    catalog_url             TEXT NOT NULL,
    acquisition_method      TEXT NOT NULL,  -- 'hosted_primary' | 'local_fallback'
    status                  TEXT NOT NULL,  -- 'RUNNING' | 'SUCCEEDED' | 'FAILED'
    record_count            INTEGER,
    raw_size_bytes          INTEGER,
    compressed_size_bytes   INTEGER,
    sha256                  TEXT,
    duration_seconds        REAL,
    error                   TEXT
);

-- The actual installed SKILL.md bundles -- always exactly one copy on
-- disk, inside the TARS Obsidian vault's Skills directory (see
-- skill_registry/manager.py's module docstring for the one-source-of-truth
-- rationale). `identifier` matches skill_catalog.identifier when installed
-- from the catalog; a direct/manual install may use a synthesized one.
CREATE TABLE IF NOT EXISTS installed_skills (
    identifier      TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'Other',
    local_path      TEXT NOT NULL,   -- relative to the vault Skills root
    source          TEXT,
    trust_level     TEXT,
    content_hash    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'installed',  -- 'installed' | 'quarantined' | 'uninstalled'
    installed_at    TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Append-only install/update/uninstall history per identifier.
CREATE TABLE IF NOT EXISTS skill_versions (
    version_id      TEXT PRIMARY KEY,
    identifier      TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    action          TEXT NOT NULL,  -- 'install' | 'update' | 'uninstall'
    notes           TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_skill_versions_identifier ON skill_versions(identifier, created_at);

-- Security/validation audit trail (Phase 8/12) -- one row per
-- quarantine-validation pass, whether it succeeded or not.
CREATE TABLE IF NOT EXISTS skill_audit (
    audit_id        TEXT PRIMARY KEY,
    identifier      TEXT NOT NULL,
    checked_at      TEXT NOT NULL,
    passed          INTEGER NOT NULL,  -- 0/1
    findings        TEXT NOT NULL DEFAULT '[]',  -- JSON array of strings
    quarantine_path TEXT
);

CREATE INDEX IF NOT EXISTS idx_skill_audit_identifier ON skill_audit(identifier, checked_at);
