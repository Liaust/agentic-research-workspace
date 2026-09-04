PRAGMA foreign_keys = ON;

ALTER TABLE cross_reference_batches
    ADD COLUMN strategy TEXT NOT NULL DEFAULT 'pairwise-v1';

ALTER TABLE cross_reference_jobs
    ADD COLUMN scope_sources_json TEXT;

