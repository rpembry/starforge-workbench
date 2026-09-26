CREATE TABLE flow_evidence_commits(
 work_item_id TEXT NOT NULL REFERENCES flow_work_items(work_item_id),
 repository_url TEXT NOT NULL,
 commit_sha TEXT NOT NULL,
 linked_at TEXT NOT NULL,
 linked_by TEXT NOT NULL,
 provenance TEXT NOT NULL CHECK(provenance='operator_reported'),
 PRIMARY KEY(work_item_id,repository_url,commit_sha)
);
