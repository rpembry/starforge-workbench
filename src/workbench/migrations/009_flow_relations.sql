CREATE TABLE flow_work_items(
 work_item_id TEXT PRIMARY KEY,
 source_ref TEXT NOT NULL UNIQUE,
 document_revision TEXT NOT NULL,
 document_hash TEXT NOT NULL,
 task_ids TEXT NOT NULL,
 projection_state TEXT NOT NULL CHECK(projection_state='proposed'),
 version INTEGER NOT NULL,
 updated_at TEXT NOT NULL,
 updated_by TEXT NOT NULL
);
CREATE TABLE flow_action_links(
 work_item_id TEXT NOT NULL REFERENCES flow_work_items(work_item_id),
 action_id TEXT NOT NULL REFERENCES actions(id),
 linked_document_revision TEXT NOT NULL,
 linked_action_version INTEGER NOT NULL,
 linked_at TEXT NOT NULL,
 linked_by TEXT NOT NULL,
 PRIMARY KEY(work_item_id, action_id)
);
CREATE INDEX flow_action_links_action ON flow_action_links(action_id);
CREATE TABLE flow_publication_ops(
 operation_id TEXT PRIMARY KEY,
 request_hash TEXT NOT NULL,
 response_json TEXT NOT NULL
);
