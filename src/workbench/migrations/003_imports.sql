CREATE TABLE import_batches(
 id TEXT PRIMARY KEY, dataset TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
 phase TEXT NOT NULL, manifest_sha256 TEXT NOT NULL, record_count INTEGER NOT NULL,
 created_at TEXT NOT NULL, recorded_by TEXT NOT NULL, UNIQUE(dataset, snapshot_sha256, phase)
);
CREATE TABLE import_records(
 id TEXT PRIMARY KEY, dataset TEXT NOT NULL, legacy_table TEXT NOT NULL, legacy_id INTEGER NOT NULL,
 row_sha256 TEXT NOT NULL, source_sha256 TEXT NOT NULL, disposition TEXT NOT NULL,
 target_resource TEXT, target_id TEXT, payload TEXT NOT NULL, metadata TEXT NOT NULL,
 UNIQUE(dataset, legacy_table, legacy_id)
);
CREATE TABLE import_batch_records(
 batch_id TEXT NOT NULL REFERENCES import_batches(id), record_id TEXT NOT NULL REFERENCES import_records(id),
 PRIMARY KEY(batch_id, record_id)
);
