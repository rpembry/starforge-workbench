CREATE TABLE report_suggestions (
    kind TEXT PRIMARY KEY,
    snapshot_hash TEXT,
    result TEXT,
    completed_at REAL NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL DEFAULT 0,
    lease_id TEXT,
    failure INTEGER NOT NULL DEFAULT 0
);
