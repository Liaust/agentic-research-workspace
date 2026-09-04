PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS cross_reference_batches (
    batch_id TEXT PRIMARY KEY,
    sources_json TEXT NOT NULL,
    input_fingerprints_json TEXT NOT NULL,
    requested_through TEXT NOT NULL,
    model TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cross_reference_jobs (
    job_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES cross_reference_batches(batch_id),
    kind TEXT NOT NULL,
    source_left TEXT NOT NULL REFERENCES sources(source_id),
    source_right TEXT NOT NULL REFERENCES sources(source_id),
    attempt INTEGER NOT NULL CHECK(attempt > 0),
    state TEXT NOT NULL,
    receipt_path TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(source_left < source_right),
    UNIQUE(batch_id, kind, source_left, source_right, attempt)
);

CREATE TABLE IF NOT EXISTS cross_reference_candidates (
    candidate_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES cross_reference_batches(batch_id),
    source_left TEXT NOT NULL REFERENCES sources(source_id),
    source_right TEXT NOT NULL REFERENCES sources(source_id),
    left_endpoint_id TEXT NOT NULL,
    left_revision INTEGER NOT NULL CHECK(left_revision > 0),
    left_record_sha256 TEXT NOT NULL,
    right_endpoint_id TEXT NOT NULL,
    right_revision INTEGER NOT NULL CHECK(right_revision > 0),
    right_record_sha256 TEXT NOT NULL,
    comparison_surface TEXT NOT NULL,
    state TEXT NOT NULL,
    discovery_job_id TEXT NOT NULL REFERENCES cross_reference_jobs(job_id),
    inspection_job_id TEXT REFERENCES cross_reference_jobs(job_id),
    outcome_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(source_left < source_right),
    UNIQUE(
        batch_id,
        left_endpoint_id,
        left_revision,
        left_record_sha256,
        right_endpoint_id,
        right_revision,
        right_record_sha256
    )
);

CREATE TABLE IF NOT EXISTS cross_reference_relationships (
    relationship_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE REFERENCES cross_reference_candidates(candidate_id),
    batch_id TEXT NOT NULL REFERENCES cross_reference_batches(batch_id),
    revision INTEGER NOT NULL CHECK(revision > 0),
    relation_type TEXT NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    inspection_job_id TEXT NOT NULL REFERENCES cross_reference_jobs(job_id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cross_reference_artifacts (
    artifact_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES cross_reference_batches(batch_id),
    job_id TEXT REFERENCES cross_reference_jobs(job_id),
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(batch_id, kind, path, sha256)
);

CREATE TABLE IF NOT EXISTS cross_reference_transitions (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL CHECK(entity_type IN ('batch', 'job', 'candidate')),
    entity_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    applied INTEGER NOT NULL CHECK(applied IN (0, 1)),
    batch_id TEXT NOT NULL REFERENCES cross_reference_batches(batch_id),
    job_id TEXT REFERENCES cross_reference_jobs(job_id),
    receipt_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cross_reference_jobs_batch
    ON cross_reference_jobs(batch_id, kind, source_left, source_right);

CREATE INDEX IF NOT EXISTS idx_cross_reference_candidates_batch_state
    ON cross_reference_candidates(batch_id, state, source_left, source_right);

CREATE INDEX IF NOT EXISTS idx_cross_reference_relationships_batch
    ON cross_reference_relationships(batch_id, relation_type);
