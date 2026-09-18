CREATE TABLE registered_sessions(
 id TEXT PRIMARY KEY,
 owner TEXT NOT NULL,
 collector_source TEXT NOT NULL REFERENCES collectors(source),
 host TEXT NOT NULL,
 display_name TEXT NOT NULL,
 provider TEXT NOT NULL,
 run_id TEXT REFERENCES runs(id),
 action_id TEXT REFERENCES actions(id),
 evidence_state TEXT NOT NULL CHECK(evidence_state IN ('present','attention_needed','provider_error','stopped','unknown')),
 reason TEXT NOT NULL,
 summary TEXT NOT NULL,
 observation_sequence INTEGER NOT NULL CHECK(observation_sequence >= 1),
 observed_at TEXT NOT NULL,
 last_activity_at TEXT,
 heartbeat_at TEXT NOT NULL,
 created_at TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX registered_sessions_owner ON registered_sessions(owner);
CREATE INDEX registered_sessions_collector ON registered_sessions(collector_source);
CREATE INDEX registered_sessions_heartbeat ON registered_sessions(heartbeat_at);
