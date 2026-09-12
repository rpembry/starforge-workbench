CREATE TABLE provider_attention_incidents(
 provider TEXT NOT NULL, session_id TEXT NOT NULL, generation_id TEXT NOT NULL,
 incident_id TEXT NOT NULL, reason TEXT NOT NULL, state TEXT NOT NULL,
 opened_at TEXT NOT NULL, last_observed_at TEXT NOT NULL, resolved_at TEXT,
 open_provenance TEXT NOT NULL, resolution_provenance TEXT,
 last_sequence INTEGER NOT NULL, recorded_at TEXT NOT NULL, recorded_by TEXT NOT NULL,
 PRIMARY KEY(provider, session_id, generation_id, incident_id)
);
CREATE INDEX provider_attention_incidents_current
 ON provider_attention_incidents(provider, session_id, generation_id, state);
