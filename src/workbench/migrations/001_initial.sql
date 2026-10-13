CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE objectives(id TEXT PRIMARY KEY, title TEXT NOT NULL, details TEXT NOT NULL, project TEXT, autonomy_ceiling INTEGER NOT NULL CHECK(autonomy_ceiling BETWEEN 0 AND 3), created_at TEXT NOT NULL);
CREATE TABLE actions(
 id TEXT PRIMARY KEY, title TEXT NOT NULL, details TEXT NOT NULL, project TEXT,
 objective_id TEXT REFERENCES objectives(id), status TEXT NOT NULL,
 execution_mode TEXT NOT NULL, actor TEXT NOT NULL, priority INTEGER NOT NULL,
 due_date TEXT, autonomy_ceiling INTEGER NOT NULL, required_level INTEGER NOT NULL,
 source TEXT NOT NULL, source_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1, UNIQUE(source, source_id)
);
CREATE TABLE runs(
 id TEXT PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL, context TEXT NOT NULL,
 provider TEXT NOT NULL, actor TEXT NOT NULL, status TEXT NOT NULL,
 action_id TEXT REFERENCES actions(id), objective_id TEXT REFERENCES objectives(id),
 started_at TEXT NOT NULL, last_activity_at TEXT, activity_basis TEXT NOT NULL,
 autonomy_ceiling INTEGER NOT NULL, required_level INTEGER NOT NULL,
 heartbeat_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, UNIQUE(source, source_id)
);
CREATE TABLE events(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, summary TEXT NOT NULL, details TEXT NOT NULL,
 project TEXT, action_id TEXT REFERENCES actions(id), run_id TEXT REFERENCES runs(id),
 source TEXT NOT NULL, source_id TEXT NOT NULL, occurred_at TEXT NOT NULL,
 recorded_at TEXT NOT NULL, recorded_by TEXT NOT NULL, UNIQUE(source, source_id)
);
CREATE TRIGGER events_immutable_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'Events are immutable'); END;
CREATE TRIGGER events_immutable_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'Events are immutable'); END;
CREATE TABLE artifacts(id TEXT PRIMARY KEY, title TEXT NOT NULL, uri TEXT NOT NULL, action_id TEXT REFERENCES actions(id), event_id TEXT REFERENCES events(id), media_type TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX actions_view ON actions(status, priority, due_date);
CREATE INDEX runs_heartbeat ON runs(heartbeat_at);
CREATE INDEX events_recent ON events(occurred_at);
-- Legacy import is a later, separately verified step. Stable legacy IDs will use
-- source/source_id; import_batches will record source snapshot hashes and counts.
-- Autonomy levels are advisory: there is no execution or policy engine here.
