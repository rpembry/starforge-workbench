CREATE TABLE provider_attention(
 provider TEXT NOT NULL, session_id TEXT NOT NULL, generation_id TEXT NOT NULL,
 generation_started_at TEXT NOT NULL, source TEXT NOT NULL, source_instance TEXT NOT NULL,
 generation_provenance TEXT NOT NULL, last_sequence INTEGER NOT NULL DEFAULT 0,
 reason TEXT, observed_at TEXT, observation_provenance TEXT,
 recorded_at TEXT NOT NULL, recorded_by TEXT NOT NULL,
 PRIMARY KEY(provider, session_id)
);
CREATE INDEX provider_attention_observed ON provider_attention(observed_at);
