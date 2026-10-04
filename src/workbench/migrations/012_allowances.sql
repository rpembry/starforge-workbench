CREATE TABLE allowance_observations (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    product TEXT NOT NULL,
    profile TEXT NOT NULL,
    bucket TEXT NOT NULL,
    window TEXT NOT NULL,
    remaining_percent REAL,
    reset_at TEXT,
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind = 'manual'),
    UNIQUE(provider, product, profile, bucket, window, observed_at)
);
CREATE INDEX allowance_latest ON allowance_observations(provider, product, profile, bucket, window, observed_at DESC);
