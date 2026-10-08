CREATE INDEX IF NOT EXISTS runs_action_heartbeat ON runs(action_id, heartbeat_at DESC);
CREATE INDEX IF NOT EXISTS runs_status_heartbeat ON runs(status, heartbeat_at DESC);
