CREATE TABLE instructions(
 id TEXT PRIMARY KEY,
 operator_principal TEXT NOT NULL,
 idempotency_key TEXT NOT NULL,
 registered_session_id TEXT NOT NULL REFERENCES registered_sessions(id),
 instruction_text TEXT NOT NULL,
 expiry_minutes INTEGER NOT NULL CHECK(expiry_minutes BETWEEN 1 AND 60),
 state TEXT NOT NULL CHECK(state IN ('queued','claimed','received','responded','failed','expired','uncertain')),
 expires_at TEXT NOT NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 claimed_at TEXT,
 lease_until TEXT,
 lease_token_hash TEXT,
 claim_owner TEXT,
 attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
 received_at TEXT,
 responded_at TEXT,
 terminal_at TEXT,
 reason_code TEXT,
 UNIQUE(operator_principal, idempotency_key)
);
CREATE INDEX instructions_session_created ON instructions(registered_session_id, created_at DESC);
CREATE INDEX instructions_claimable ON instructions(registered_session_id, state, created_at);

CREATE TABLE instruction_audit(
 id TEXT PRIMARY KEY,
 instruction_id TEXT NOT NULL REFERENCES instructions(id),
 actor TEXT NOT NULL,
 registered_session_id TEXT NOT NULL,
 state TEXT NOT NULL,
 reason_code TEXT NOT NULL,
 occurred_at TEXT NOT NULL
);
CREATE INDEX instruction_audit_history ON instruction_audit(instruction_id, occurred_at, id);
CREATE TRIGGER instruction_audit_immutable_update BEFORE UPDATE ON instruction_audit
BEGIN SELECT RAISE(ABORT, 'Instruction audit records are immutable'); END;
CREATE TRIGGER instruction_audit_immutable_delete BEFORE DELETE ON instruction_audit
BEGIN SELECT RAISE(ABORT, 'Instruction audit records are immutable'); END;
