CREATE TABLE collectors(
 id TEXT PRIMARY KEY, source TEXT NOT NULL UNIQUE, instance_id TEXT NOT NULL,
 scope TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('ok','degraded')),
 reason TEXT NOT NULL, observed_runs INTEGER NOT NULL CHECK(observed_runs >= 0),
 heartbeat_at TEXT NOT NULL, last_success_at TEXT, recorded_by TEXT NOT NULL
);
