"""SQLite repository boundary. Only the service opens the database."""
from __future__ import annotations
import json
import hashlib
import hmac
import os
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from .models import now


class Problem(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


class Repository(Protocol):
    def list(self, resource: str, limit: int = 100, offset: int = 0) -> list[dict]: ...
    def get(self, resource: str, identity: str) -> dict: ...
    def create(self, resource: str, data: dict, principal: str) -> dict: ...
    def patch(self, resource: str, identity: str, data: dict, principal: str) -> dict: ...
    def link_run(self, action_id: str, run_id: str, data: dict, principal: str) -> dict: ...
    def publish_work_item(self, data: dict, principal: str) -> dict: ...
    def link_work_item_action(self, work_item_id: str, action_id: str, data: dict, principal: str) -> dict: ...
    def get_work_item(self, work_item_id: str) -> dict: ...
    def list_work_items(self, limit: int = 100, offset: int = 0) -> list[dict]: ...


TABLES = {'objectives', 'actions', 'runs', 'events', 'artifacts', 'collectors', 'registered_sessions', 'import_batches', 'import_records'}
TRANSITIONS = {
    'observed': {'proposed', 'rejected'},
    'proposed': {'accepted', 'rejected', 'approval_needed'},
    'accepted': {'in_progress', 'done', 'waiting', 'approval_needed', 'canceled'},
    'in_progress': {'done', 'waiting', 'approval_needed', 'canceled'},
    'waiting': {'accepted', 'in_progress', 'done', 'canceled'},
    'approval_needed': {'accepted', 'rejected', 'canceled'},
    'done': set(), 'rejected': set(), 'canceled': set(),
}

SESSION_STALE_AFTER_SECONDS = 30
SESSION_HOST_OFFLINE_AFTER_SECONDS = 90
SESSION_MAX_FUTURE_SKEW_SECONDS = 300
INSTRUCTION_LEASE_SECONDS = 30
INSTRUCTION_PRINCIPAL_RATE_LIMIT = 10
INSTRUCTION_SESSION_RATE_LIMIT = 5
INSTRUCTION_RATE_WINDOW_SECONDS = 60
CONTROLLABLE_SESSION_PROVIDERS = {'opencode'}


class SQLiteRepository:
    def __init__(self, path: Path):
        self.path = path
        if path.is_symlink() or path.parent.is_symlink():
            raise RuntimeError('Database and state directory must not be symlinks')
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.stat().st_uid != os.getuid() or path.parent.stat().st_mode & 0o077:
            raise RuntimeError('Database directory must be owned by this user with mode 0700')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise RuntimeError('Database file must be owned by this user with mode 0600')
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone()
            if not exists:
                migration = Path(__file__).with_name('migrations')/'001_initial.sql'
                db.executescript('BEGIN IMMEDIATE;\n'+migration.read_text())
                db.execute('INSERT INTO schema_migrations VALUES (?, ?)', (1, now()))
                db.commit()
            versions = [r[0] for r in db.execute('SELECT version FROM schema_migrations ORDER BY version')]
            supported = [list(range(1, version + 1)) for version in range(1, 10)]
            if versions not in supported:
                raise RuntimeError('Unsupported database schema version')
            for version, filename in [(2, '002_collectors.sql'), (3, '003_imports.sql'),
                                      (4, '004_provider_attention.sql'),
                                      (5, '005_provider_attention_incidents.sql'),
                                       (6, '006_report_suggestions.sql'), (7, '007_registered_sessions.sql'),
                                       (8, '008_instructions.sql'), (9, '009_flow_relations.sql')]:
                if version not in versions:
                    migration = Path(__file__).with_name('migrations')/filename
                    db.executescript('BEGIN IMMEDIATE;\n'+migration.read_text())
                    db.execute('INSERT INTO schema_migrations VALUES (?, ?)', (version, now()))
                    db.commit()

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def table(resource):
        if resource not in TABLES:
            raise Problem(404, 'not_found', 'Unknown resource')
        return resource

    def list(self, resource, limit=100, offset=0):
        table = self.table(resource)
        order = 'occurred_at DESC, id' if table == 'events' else 'rowid DESC'
        with self.connection() as db:
            return [dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY {order} LIMIT ? OFFSET ?', (limit, offset))]

    def get(self, resource, identity):
        with self.connection() as db:
            return self._get(db, resource, identity)

    def _get(self, db, resource, identity):
        row = db.execute(f'SELECT * FROM {self.table(resource)} WHERE id=?', (identity,)).fetchone()
        if not row:
            raise Problem(404, 'not_found', 'Resource does not exist')
        return dict(row)

    @staticmethod
    def insert(db, table, data):
        columns = ','.join(data)
        db.execute(f'INSERT INTO {table} ({columns}) VALUES ({",".join("?" for _ in data)})', tuple(data.values()))

    def audit(self, db, action_id, summary, principal, details=''):
        self.insert(db, 'events', dict(id=str(uuid.uuid4()), kind='action_transition', summary=summary,
            details=details, project=None, action_id=action_id, run_id=None, source='workbench-api',
            source_id=str(uuid.uuid4()), occurred_at=now(), recorded_at=now(), recorded_by=principal))

    def create(self, resource, data, principal):
        table = self.table(resource)
        data = dict(data)
        with self.connection() as db:
            try:
                db.execute('BEGIN IMMEDIATE')
                # Stable collector IDs deduplicate retries. Changed immutable observations
                # and action intent conflict instead of silently overwriting a previous fact.
                if table in {'events', 'actions', 'runs'} and data.get('source_id'):
                    row = db.execute(f'SELECT * FROM {table} WHERE source=? AND source_id=?', (data['source'], data['source_id'])).fetchone()
                    if row:
                        if table == 'runs':
                            if row['context'] != data['context'] or row['provider'] != data['provider']:
                                raise Problem(409, 'identity_conflict', 'Run identity cannot change context or provider')
                            if data['started_at'] != row['started_at']:
                                raise Problem(409, 'identity_conflict', 'A process restart requires a new run identity')
                            # A heartbeat cannot erase operator-assigned work or an
                            # explicit waiting/approval state with process-presence data.
                            update = {k: data[k] for k in ['last_activity_at', 'activity_basis']}
                            if row['status'] not in {'waiting', 'approval_needed'}:
                                update['status'] = data['status']
                            update.update(heartbeat_at=now(), version=row['version']+1)
                            self._update(db, table, row['id'], update)
                        elif any(row[k] != v for k, v in data.items() if not (k == 'occurred_at' and v is None)):
                            raise Problem(409, 'idempotency_conflict', 'Source ID already exists with different content')
                        result = self._get(db, table, row['id'])
                        db.commit()
                        return result
                data['id'] = str(uuid.uuid4())
                if table == 'events':
                    data.update(occurred_at=data.get('occurred_at') or now(), recorded_at=now(), recorded_by=principal)
                elif table == 'runs':
                    data.update(heartbeat_at=now(), version=1)
                else:
                    data['created_at'] = now()
                    if table == 'actions':
                        data.update(updated_at=now(), version=1)
                self.insert(db, table, data)
                if table == 'actions':
                    self.audit(db, data['id'], 'Action '+data['status'], principal)
                db.commit()
                return data
            except sqlite3.IntegrityError:
                raise Problem(409, 'constraint_conflict', 'Related resource missing or identity already exists') from None

    @staticmethod
    def _update(db, table, identity, data):
        db.execute(f'UPDATE {table} SET {",".join(k+"=?" for k in data)} WHERE id=?', (*data.values(), identity))

    def patch(self, resource, identity, data, principal):
        if resource not in {'actions', 'runs'}:
            raise Problem(405, 'immutable', 'This resource cannot be edited')
        data = dict(data)
        with self.connection() as db:
            try:
                db.execute('BEGIN IMMEDIATE')
                old = self._get(db, resource, identity)
                version = data.pop('version')
                if old['version'] != version:
                    raise Problem(409, 'version_conflict', 'Read the latest resource before updating it')
                if resource == 'actions':
                    target = data.get('status', old['status'])
                    if target != old['status'] and target not in TRANSITIONS[old['status']]:
                        raise Problem(409, 'invalid_transition', f'Cannot move {old["status"]} to {target}')
                    data.update(updated_at=now())
                    self.audit(db, identity, 'Action '+target, principal,
                               json.dumps({'from': old['status'], 'to': target, 'changed_fields': sorted(data)}))
                data['version'] = version+1
                self._update(db, resource, identity, data)
                result = self._get(db, resource, identity)
                db.commit()
                return result
            except sqlite3.IntegrityError:
                raise Problem(409, 'constraint_conflict', 'Related resource missing or invalid field') from None

    def link_run(self, action_id, run_id, data, principal):
        from datetime import datetime, timezone
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            action = self._get(db, 'actions', action_id)
            run = self._get(db, 'runs', run_id)
            if action['version'] != data['action_version'] or run['version'] != data['run_version']:
                raise Problem(409, 'version_conflict', 'Read both current records before linking them')
            if action['execution_mode'] != 'agent' or action['status'] not in {
                    'accepted', 'in_progress', 'waiting', 'approval_needed'}:
                raise Problem(409, 'incompatible_action', 'Linking requires a committed agent action')
            age = (datetime.now(timezone.utc)-datetime.fromisoformat(run['heartbeat_at'])).total_seconds()
            if age > 90 or run['status'] in {'stopped', 'unknown'}:
                raise Problem(409, 'incompatible_run', 'Linking requires a fresh non-stopped run')
            previous = run['action_id']
            expected = data.get('replace_action_id')
            if previous == action_id:
                if expected not in {None, action_id}:
                    raise Problem(409, 'assignment_conflict', 'Replacement identity does not match the current link')
                return {'changed': False, 'action': action, 'run': run, 'audit_event': None}
            if previous and expected != previous:
                raise Problem(409, 'assignment_conflict', 'Run is already linked; name its current action to reassign it')
            if not previous and expected is not None:
                raise Problem(409, 'assignment_conflict', 'Unlinked run does not match the requested replacement')
            self._update(db, 'runs', run_id, {'action_id': action_id, 'version': run['version']+1})
            linked = self._get(db, 'runs', run_id)
            event = dict(id=str(uuid.uuid4()), kind='decision', summary='Run linked to action',
                details=json.dumps({'action_id': action_id, 'action_version': action['version'],
                    'run_id': run_id, 'run_version': linked['version'], 'previous_action_id': previous,
                    'run_source': run['source'], 'run_source_id': run['source_id'],
                    'run_started_at': run['started_at']}, sort_keys=True), project=action['project'],
                action_id=action_id, run_id=run_id, source='workbench-api', source_id=str(uuid.uuid4()),
                occurred_at=now(), recorded_at=now(), recorded_by=principal)
            self.insert(db, 'events', event)
            db.commit()
            return {'changed': True, 'action': action, 'run': linked, 'audit_event': event}

    def publish_work_item(self, data, principal):
        from .flow_relations import publish_work_item
        return publish_work_item(self, data, principal)

    def link_work_item_action(self, work_item_id, action_id, data, principal):
        from .flow_relations import link_work_item_action
        return link_work_item_action(self, work_item_id, action_id, data, principal)

    def get_work_item(self, work_item_id):
        from .flow_relations import get_work_item
        return get_work_item(self, work_item_id)

    def list_work_items(self, limit=100, offset=0):
        from .flow_relations import list_work_items
        return list_work_items(self, limit, offset)

    def import_batch(self, data, principal):
        from .legacy import apply_batch
        return apply_batch(self, data, principal)

    def collector_heartbeat(self, data, principal):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM collectors WHERE source=?', (data['source'],)).fetchone()
            if old and old['recorded_by'] != principal:
                raise Problem(403, 'collector_owner', 'Collector identity belongs to another principal')
            stamp = now()
            row = dict(data, id=old['id'] if old else str(uuid.uuid4()), heartbeat_at=stamp,
                       last_success_at=stamp if data['status'] == 'ok' else (old['last_success_at'] if old else None),
                       recorded_by=principal)
            if old:
                self._update(db, 'collectors', old['id'], row)
            else:
                self.insert(db, 'collectors', row)
            if not old or old['status'] != row['status'] or old['instance_id'] != row['instance_id']:
                self.insert(db, 'events', dict(id=str(uuid.uuid4()), kind='observation',
                    summary='Collector '+row['source']+': '+row['status'], details=row['reason'],
                    project='AI Workbench', action_id=None, run_id=None, source='collector-health',
                    source_id=str(uuid.uuid4()), occurred_at=stamp, recorded_at=stamp, recorded_by=principal))
            db.commit()
            return row

    def register_session(self, data, principal):
        """Create/update only the same collector-owned opaque registration."""
        from datetime import datetime, timedelta, timezone
        data = dict(data)
        observed = datetime.fromisoformat(data['observed_at'])
        if observed > datetime.now(timezone.utc) + timedelta(seconds=SESSION_MAX_FUTURE_SKEW_SECONDS):
            raise Problem(422, 'future_observation', 'Observation time is too far in the future')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM registered_sessions WHERE id=?', (data['id'],)).fetchone()
            if old:
                if old['owner'] != principal:
                    raise Problem(403, 'registered_session_owner', 'Registration belongs to another collector principal')
                if old['collector_source'] != data['collector_source']:
                    raise Problem(409, 'registration_identity', 'Collector source cannot change')
                if old['evidence_state'] == 'stopped' and data['evidence_state'] != 'stopped':
                    raise Problem(409, 'stopped_registration', 'A stopped registration cannot become controllable again')
            collector = db.execute('SELECT recorded_by FROM collectors WHERE source=?',
                                   (data['collector_source'],)).fetchone()
            if not collector or collector['recorded_by'] != principal:
                raise Problem(403, 'collector_owner', 'Registration requires an owned collector heartbeat')
            if data['action_id']:
                if not data['run_id']:
                    raise Problem(409, 'unverified_action', 'Action association requires a linked run')
                run = self._get(db, 'runs', data['run_id'])
                if run['action_id'] != data['action_id']:
                    raise Problem(409, 'unverified_action', 'Action is not linked to this run')
            elif data['run_id']:
                self._get(db, 'runs', data['run_id'])
            if old:
                if data['observation_sequence'] <= old['observation_sequence']:
                    raise Problem(409, 'observation_sequence', 'Observation sequence must increase')
                if observed <= datetime.fromisoformat(old['observed_at']):
                    raise Problem(409, 'observation_clock', 'Observation time must increase')
                if any(old[k] != data[k] for k in ('host', 'display_name', 'provider')):
                    raise Problem(409, 'registration_identity', 'Registered session identity cannot change')
                update = dict(data, heartbeat_at=now(), version=old['version'] + 1)
                self._update(db, 'registered_sessions', data['id'], update)
            else:
                data.update(owner=principal, heartbeat_at=now(), created_at=now(), version=1)
                self.insert(db, 'registered_sessions', data)
            row = self._registered_session_row(db, data['id'])
            db.commit()
            return self._session_visibility(row)

    @staticmethod
    def _registered_session_row(db, identity):
        row = db.execute('''SELECT s.*, c.heartbeat_at AS host_heartbeat_at
            FROM registered_sessions s JOIN collectors c ON c.source=s.collector_source
            WHERE s.id=?''', (identity,)).fetchone()
        if not row:
            raise Problem(404, 'not_found', 'Resource does not exist')
        return row

    @staticmethod
    def _session_visibility(row):
        from datetime import datetime, timezone
        row = dict(row)
        row.pop('owner', None)  # Collector principal is authorization state, not public session metadata.
        host_heartbeat_at = row.pop('host_heartbeat_at')
        row.pop('collector_source', None)
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row['heartbeat_at'])).total_seconds()
        host_age = (datetime.now(timezone.utc) - datetime.fromisoformat(host_heartbeat_at)).total_seconds()
        row['heartbeat_age_seconds'] = max(0, int(age))
        row['visibility'] = ('offline' if host_age > SESSION_HOST_OFFLINE_AFTER_SECONDS
                             else 'stale' if age > SESSION_STALE_AFTER_SECONDS else 'fresh')
        return row

    def list_registered_sessions(self, limit=100, offset=0):
        with self.connection() as db:
            rows = db.execute('''SELECT s.*, c.heartbeat_at AS host_heartbeat_at
                FROM registered_sessions s JOIN collectors c ON c.source=s.collector_source
                ORDER BY s.heartbeat_at DESC, s.id LIMIT ? OFFSET ?''', (limit, offset)).fetchall()
            return [self._session_visibility(row) for row in rows]

    def get_registered_session(self, identity):
        with self.connection() as db:
            return self._session_visibility(self._registered_session_row(db, identity))

    @staticmethod
    def _instruction_public(row, include_text=True):
        row = dict(row)
        for key in ('operator_principal', 'lease_token_hash', 'claim_owner'):
            row.pop(key, None)
        if not include_text:
            row.pop('instruction_text', None)
        elif 'instruction_text' in row:
            row['text'] = row.pop('instruction_text')
        return row

    @staticmethod
    def _instruction_audit(db, instruction_id, actor, session_id, state, reason_code, stamp):
        SQLiteRepository.insert(db, 'instruction_audit', dict(
            id=str(uuid.uuid4()), instruction_id=instruction_id, actor=actor,
            registered_session_id=session_id, state=state, reason_code=reason_code,
            occurred_at=stamp))

    @staticmethod
    def _expire_instruction_leases(db, stamp):
        queued = db.execute("SELECT id,registered_session_id FROM instructions WHERE state='queued' AND expires_at<=?",
                            (stamp,)).fetchall()
        for row in queued:
            db.execute("UPDATE instructions SET state='expired',updated_at=?,terminal_at=?,reason_code='instruction_expired' WHERE id=?",
                       (stamp, stamp, row['id']))
            SQLiteRepository._instruction_audit(db, row['id'], 'workbench-server',
                                                row['registered_session_id'], 'expired',
                                                'instruction_expired', stamp)
        abandoned = db.execute("SELECT id,registered_session_id FROM instructions WHERE state='claimed' AND lease_until<=?",
                               (stamp,)).fetchall()
        for row in abandoned:
            db.execute("UPDATE instructions SET state='uncertain',updated_at=?,terminal_at=?,reason_code='lease_expired',lease_until=NULL WHERE id=?",
                       (stamp, stamp, row['id']))
            SQLiteRepository._instruction_audit(db, row['id'], 'workbench-server',
                                                row['registered_session_id'], 'uncertain',
                                                'lease_expired', stamp)

    @staticmethod
    def _controllable_session(db, identity, owner=None):
        row = SQLiteRepository._registered_session_row(db, identity)
        if owner is not None and row['owner'] != owner:
            raise Problem(404, 'not_found', 'Registered session does not exist')
        visible = SQLiteRepository._session_visibility(row)
        if visible['visibility'] != 'fresh':
            raise Problem(409, 'target_unavailable', 'Registered session is not fresh')
        if row['evidence_state'] in {'unknown', 'stopped'} or row['provider'] not in CONTROLLABLE_SESSION_PROVIDERS:
            raise Problem(409, 'target_uncontrollable', 'Registered session is not controllable')
        return row

    def create_instruction(self, data, principal):
        from datetime import datetime, timedelta, timezone
        data = dict(data)
        text = data.pop('text')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM instructions WHERE operator_principal=? AND idempotency_key=?',
                                  (principal, data['idempotency_key'])).fetchone()
            if existing:
                matches = (existing['registered_session_id'] == data['registered_session_id']
                           and existing['instruction_text'] == text
                           and existing['expiry_minutes'] == data['expiry_minutes'])
                if not matches:
                    raise Problem(409, 'idempotency_conflict', 'Idempotency key already has different instruction metadata')
                db.commit()
                return self._instruction_public(existing)
            self._controllable_session(db, data['registered_session_id'])
            current = datetime.now(timezone.utc)
            stamp = current.isoformat()
            cutoff = (current - timedelta(seconds=INSTRUCTION_RATE_WINDOW_SECONDS)).isoformat()
            principal_count = db.execute('SELECT count(*) FROM instructions WHERE operator_principal=? AND created_at>?',
                                         (principal, cutoff)).fetchone()[0]
            session_count = db.execute('SELECT count(*) FROM instructions WHERE registered_session_id=? AND created_at>?',
                                       (data['registered_session_id'], cutoff)).fetchone()[0]
            if principal_count >= INSTRUCTION_PRINCIPAL_RATE_LIMIT:
                raise Problem(429, 'principal_rate_limited', 'Instruction creation rate exceeded')
            if session_count >= INSTRUCTION_SESSION_RATE_LIMIT:
                raise Problem(429, 'session_rate_limited', 'Instruction creation rate exceeded')
            row = dict(id=str(uuid.uuid4()), operator_principal=principal,
                       idempotency_key=data['idempotency_key'],
                       registered_session_id=data['registered_session_id'], instruction_text=text,
                       expiry_minutes=data['expiry_minutes'], state='queued',
                       expires_at=(current + timedelta(minutes=data['expiry_minutes'])).isoformat(),
                       created_at=stamp, updated_at=stamp, claimed_at=None, lease_until=None,
                       lease_token_hash=None, claim_owner=None, attempt_count=0, received_at=None,
                       responded_at=None, terminal_at=None, reason_code=None)
            self.insert(db, 'instructions', row)
            self._instruction_audit(db, row['id'], principal, row['registered_session_id'],
                                    'queued', 'operator_created', stamp)
            db.commit()
            return self._instruction_public(row)

    def list_instructions(self, limit=100, offset=0, session_id=None):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            stamp = now()
            self._expire_instruction_leases(db, stamp)
            if session_id:
                rows = db.execute('''SELECT * FROM instructions WHERE registered_session_id=?
                    ORDER BY created_at DESC,id LIMIT ? OFFSET ?''', (session_id, limit, offset)).fetchall()
            else:
                rows = db.execute('SELECT * FROM instructions ORDER BY created_at DESC,id LIMIT ? OFFSET ?',
                                  (limit, offset)).fetchall()
            db.commit()
            return [self._instruction_public(row) for row in rows]

    def get_instruction(self, identity):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._expire_instruction_leases(db, now())
            row = db.execute('SELECT * FROM instructions WHERE id=?', (identity,)).fetchone()
            if not row:
                raise Problem(404, 'not_found', 'Instruction does not exist')
            history = [dict(item) for item in db.execute('''SELECT id,actor,registered_session_id,state,reason_code,occurred_at
                FROM instruction_audit WHERE instruction_id=? ORDER BY occurred_at,id''', (identity,))]
            db.commit()
            return dict(self._instruction_public(row), history=history)

    def claim_instruction(self, session_id, principal, claims_enabled=False):
        from datetime import datetime, timedelta, timezone
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if not claims_enabled:
                raise Problem(503, 'instruction_claims_disabled', 'Instruction claims are disabled')
            self._controllable_session(db, session_id, principal)
            current = datetime.now(timezone.utc)
            stamp = current.isoformat()
            self._expire_instruction_leases(db, stamp)
            row = db.execute('''SELECT * FROM instructions
                WHERE registered_session_id=? AND state='queued' AND expires_at>?
                ORDER BY created_at,id LIMIT 1''', (session_id, stamp)).fetchone()
            if not row:
                raise Problem(404, 'no_instruction', 'No queued instruction is available')
            token = secrets.token_urlsafe(32)
            lease_until = min(current + timedelta(seconds=INSTRUCTION_LEASE_SECONDS),
                              datetime.fromisoformat(row['expires_at'])).isoformat()
            changed = db.execute('''UPDATE instructions SET state='claimed',claimed_at=?,lease_until=?,
                lease_token_hash=?,claim_owner=?,attempt_count=attempt_count+1,updated_at=?,reason_code='worker_claimed'
                WHERE id=? AND state='queued' ''',
                (stamp, lease_until, hashlib.sha256(token.encode()).hexdigest(), principal, stamp, row['id']))
            if changed.rowcount != 1:
                raise Problem(409, 'claim_conflict', 'Instruction was claimed concurrently')
            self._instruction_audit(db, row['id'], principal, session_id, 'claimed', 'worker_claimed', stamp)
            claimed = db.execute('SELECT * FROM instructions WHERE id=?', (row['id'],)).fetchone()
            db.commit()
            return dict(self._instruction_public(claimed), lease_token=token)

    @staticmethod
    def _verify_instruction_lease(db, identity, token, principal):
        row = db.execute('SELECT * FROM instructions WHERE id=?', (identity,)).fetchone()
        if not row or row['claim_owner'] != principal:
            raise Problem(404, 'not_found', 'Instruction does not exist')
        supplied = hashlib.sha256(token.encode()).hexdigest()
        if not row['lease_token_hash'] or not hmac.compare_digest(row['lease_token_hash'], supplied):
            raise Problem(409, 'lease_mismatch', 'Instruction lease does not match')
        return row

    def verify_instruction_lease(self, identity, token, principal):
        """Read-only proof that `principal` holds the current lease on
        `identity`. Raises the same Problems as the other lease-checking
        flows; performs no state transition and touches no other table."""
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._verify_instruction_lease(db, identity, token, principal)
            if row['state'] != 'received':
                raise Problem(409, 'invalid_transition',
                              'Response preview requires received delivery evidence')
            db.commit()
            return {'instruction_id': row['id'],
                    'registered_session_id': row['registered_session_id']}

    def renew_instruction_claim(self, identity, token, principal, claims_enabled=False):
        from datetime import datetime, timedelta, timezone
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if not claims_enabled:
                raise Problem(503, 'instruction_claims_disabled', 'Instruction claims are disabled')
            current = datetime.now(timezone.utc)
            stamp = current.isoformat()
            self._expire_instruction_leases(db, stamp)
            row = self._verify_instruction_lease(db, identity, token, principal)
            if row['state'] != 'claimed' or row['lease_until'] <= stamp:
                raise Problem(409, 'invalid_transition', 'Only an active claim can be renewed')
            lease_until = min(current + timedelta(seconds=INSTRUCTION_LEASE_SECONDS),
                              datetime.fromisoformat(row['expires_at'])).isoformat()
            db.execute('UPDATE instructions SET lease_until=?,updated_at=? WHERE id=?',
                       (lease_until, stamp, identity))
            renewed = db.execute('SELECT * FROM instructions WHERE id=?', (identity,)).fetchone()
            db.commit()
            return self._instruction_public(renewed, include_text=False)

    def report_instruction_result(self, identity, data, principal):
        data = dict(data)
        token = data.pop('lease_token')
        outcome = data['outcome']
        reason = data['reason_code']
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            stamp = now()
            self._expire_instruction_leases(db, stamp)
            row = self._verify_instruction_lease(db, identity, token, principal)
            if outcome == 'responded':
                if row['state'] != 'received':
                    raise Problem(409, 'invalid_transition', 'Responded requires received delivery evidence')
                update = dict(state='responded', responded_at=stamp, terminal_at=stamp,
                              updated_at=stamp, reason_code=reason)
            else:
                if row['state'] != 'claimed' or row['lease_until'] <= stamp:
                    raise Problem(409, 'invalid_transition', 'Result requires an active claim')
                if outcome == 'retryable':
                    update = dict(state='queued', claimed_at=None, lease_until=None, lease_token_hash=None,
                                  claim_owner=None, updated_at=stamp, reason_code=reason)
                elif outcome == 'received':
                    update = dict(state='received', received_at=stamp, lease_until=None,
                                  updated_at=stamp, reason_code=reason)
                else:
                    update = dict(state=outcome, lease_until=None, terminal_at=stamp,
                                  updated_at=stamp, reason_code=reason)
            self._update(db, 'instructions', identity, update)
            state = update['state']
            self._instruction_audit(db, identity, principal, row['registered_session_id'], state, reason, stamp)
            result = db.execute('SELECT * FROM instructions WHERE id=?', (identity,)).fetchone()
            db.commit()
            return self._instruction_public(result, include_text=False)

    def activate_provider_generation(self, data, principal):
        from datetime import datetime, timedelta, timezone
        data = dict(data)
        data['generation_started_at'] = data.pop('started_at')
        data['generation_provenance'] = data.pop('provenance')
        started = datetime.fromisoformat(data['generation_started_at'])
        if started > datetime.now(timezone.utc)+timedelta(minutes=5):
            raise Problem(422, 'invalid_observation_clock', 'Provider generation clock is too far in the future')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM provider_attention WHERE provider=? AND session_id=?',
                             (data['provider'], data['session_id'])).fetchone()
            if old and old['recorded_by'] != principal:
                raise Problem(403, 'provider_attention_owner', 'Provider session belongs to another collector principal')
            if old and old['generation_id'] == data['generation_id']:
                immutable = ('generation_started_at', 'source', 'source_instance', 'generation_provenance')
                if any(old[key] != data[key] for key in immutable):
                    raise Problem(409, 'generation_conflict', 'Generation identity already has different provenance')
                return dict(old)
            if old and started <= datetime.fromisoformat(old['generation_started_at']):
                raise Problem(409, 'stale_generation', 'An older provider generation cannot become current')
            row = dict(data, last_sequence=0, reason=None, observed_at=None,
                       observation_provenance=None, recorded_at=now(), recorded_by=principal)
            if old:
                assignments = ','.join(key+'=:'+key for key in row if key not in {'provider', 'session_id'})
                db.execute(f'''UPDATE provider_attention SET {assignments}
                    WHERE provider=:provider AND session_id=:session_id''', row)
            else:
                self.insert(db, 'provider_attention', row)
            result = db.execute('SELECT * FROM provider_attention WHERE provider=? AND session_id=?',
                                (row['provider'], row['session_id'])).fetchone()
            db.commit()
            return dict(result)

    def record_provider_attention(self, data, principal):
        from datetime import datetime, timedelta, timezone
        data = dict(data)
        observed = datetime.fromisoformat(data['observed_at'])
        if observed > datetime.now(timezone.utc)+timedelta(minutes=5):
            raise Problem(422, 'invalid_observation_clock', 'Provider observation clock is too far in the future')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM provider_attention WHERE provider=? AND session_id=?',
                             (data['provider'], data['session_id'])).fetchone()
            if not old or old['generation_id'] != data['generation_id']:
                raise Problem(409, 'stale_generation', 'Observation is not for the authoritative current generation')
            if old['recorded_by'] != principal:
                raise Problem(403, 'provider_attention_owner', 'Provider session belongs to another collector principal')
            if old['source'] != data['source'] or old['source_instance'] != data['source_instance']:
                raise Problem(409, 'generation_conflict', 'Observation provenance does not own this generation')
            if data['sequence'] == old['last_sequence']:
                raise Problem(409, 'duplicate_observation', 'Observation sequence was already recorded')
            if data['sequence'] != old['last_sequence']+1:
                raise Problem(409, 'out_of_order_observation', 'Observation sequence does not follow current evidence')
            observed_at = data['observed_at']
            if observed < datetime.fromisoformat(old['generation_started_at']) or (old['observed_at'] and observed <= datetime.fromisoformat(old['observed_at'])):
                raise Problem(409, 'out_of_order_observation', 'Observation clock is not newer than current evidence')
            incident = db.execute('''SELECT * FROM provider_attention_incidents
                WHERE provider=? AND session_id=? AND generation_id=? AND incident_id=?''',
                (data['provider'], data['session_id'], data['generation_id'], data['incident_id'])).fetchone()
            if incident and incident['reason'] != data['reason']:
                raise Problem(409, 'incident_conflict', 'Provider incident identity has different provenance')
            if data['state'] == 'open':
                if incident and incident['state'] != 'open':
                    raise Problem(409, 'incident_closed', 'Resolved provider incident cannot reopen')
                if incident and incident['open_provenance'] != data['provenance']:
                    raise Problem(409, 'incident_conflict', 'Provider incident identity has different provenance')
                if incident:
                    db.execute('''UPDATE provider_attention_incidents SET last_observed_at=?, last_sequence=?,
                        recorded_at=?, recorded_by=? WHERE provider=? AND session_id=? AND generation_id=? AND incident_id=?''',
                        (observed_at, data['sequence'], now(), principal, data['provider'], data['session_id'],
                         data['generation_id'], data['incident_id']))
                else:
                    self.insert(db, 'provider_attention_incidents', dict(
                        provider=data['provider'], session_id=data['session_id'], generation_id=data['generation_id'],
                        incident_id=data['incident_id'], reason=data['reason'], state='open', opened_at=observed_at,
                        last_observed_at=observed_at, resolved_at=None, open_provenance=data['provenance'],
                        resolution_provenance=None, last_sequence=data['sequence'], recorded_at=now(), recorded_by=principal))
            else:
                if incident and incident['state'] != 'open':
                    raise Problem(409, 'incident_not_open', 'Provider incident is not currently open')
                if incident:
                    db.execute('''UPDATE provider_attention_incidents SET state='resolved', resolved_at=?,
                        resolution_provenance=?, last_sequence=?, recorded_at=?, recorded_by=?
                        WHERE provider=? AND session_id=? AND generation_id=? AND incident_id=?''',
                        (observed_at, data['provenance'], data['sequence'], now(), principal, data['provider'],
                         data['session_id'], data['generation_id'], data['incident_id']))
            update = dict(last_sequence=data['sequence'], reason=data['reason'], observed_at=observed_at,
                           observation_provenance=data['provenance'], recorded_at=now(), recorded_by=principal)
            db.execute('''UPDATE provider_attention SET last_sequence=:last_sequence, reason=:reason,
                observed_at=:observed_at, observation_provenance=:observation_provenance,
                recorded_at=:recorded_at, recorded_by=:recorded_by
                WHERE provider=:provider AND session_id=:session_id''',
                dict(update, provider=data['provider'], session_id=data['session_id']))
            result = db.execute('''SELECT * FROM provider_attention_incidents
                WHERE provider=? AND session_id=? AND generation_id=? AND incident_id=?''',
                (data['provider'], data['session_id'], data['generation_id'], data['incident_id'])).fetchone()
            db.commit()
            return dict(result) if result else {
                'provider': data['provider'], 'session_id': data['session_id'],
                'generation_id': data['generation_id'], 'incident_id': data['incident_id'],
                'sequence': data['sequence'], 'state': 'ignored_orphan_resolution'}

    def dashboard(self):
        with self.connection() as db:
            actions = [dict(r) for r in db.execute('SELECT * FROM actions ORDER BY priority, due_date IS NULL, due_date, created_at')]
            runs = [dict(r) for r in db.execute('SELECT * FROM runs ORDER BY heartbeat_at DESC')]
            from datetime import datetime, timezone
            current = datetime.now(timezone.utc)
            for run in runs:
                age = (current-datetime.fromisoformat(run['heartbeat_at'])).total_seconds()
                run['stale'] = age > 90
                run['heartbeat_age_seconds'] = max(0, int(age))
                run['elapsed_seconds'] = max(0, int((current-datetime.fromisoformat(run['started_at'])).total_seconds()))
            collectors = [dict(r) for r in db.execute('SELECT * FROM collectors ORDER BY source')]
            for collector in collectors:
                age = (current-datetime.fromisoformat(collector['heartbeat_at'])).total_seconds()
                collector['heartbeat_age_seconds'] = max(0, int(age))
                collector['health'] = 'offline' if age > 90 else collector['status']
            provider_attention = [dict(r) for r in db.execute('''SELECT i.*,g.generation_started_at
                FROM provider_attention_incidents i JOIN provider_attention g
                  ON g.provider=i.provider AND g.session_id=i.session_id AND g.generation_id=i.generation_id
                WHERE i.state='open' ORDER BY i.provider,i.session_id,i.opened_at,i.incident_id''')]
            for observation in provider_attention:
                stamp = observation['last_observed_at']
                age = (current-datetime.fromisoformat(stamp)).total_seconds()
                observation['freshness_age_seconds'] = max(0, int(age))
                observation['fresh'] = bool(observation['reason']) and age <= 90
            committed = {'accepted', 'in_progress', 'waiting', 'approval_needed'}
            from .attention import derive
            from .recent import present
            from .analytics import traffic_summary
            stamp = current.isoformat()
            analytics_events = [dict(r) for r in db.execute(
                "SELECT * FROM events WHERE source=? ORDER BY occurred_at DESC", ("ga4-daily-collector",)
            )]
            result = dict(generated_at=stamp, collectors=collectors,
                attention=derive(actions, runs, collectors, stamp, provider_attention),
                provider_attention=provider_attention,
                quarantined_imports=db.execute("SELECT count(*) FROM import_records WHERE disposition='quarantined'").fetchone()[0],
                needs_you={'collectors': [c for c in collectors if c['health'] != 'ok'], 'actions': [a for a in actions if a['status'] in committed and (a['execution_mode'] in {'human', 'waiting'} or a['status'] in {'waiting', 'approval_needed'})],
                           'runs': [r for r in runs if not r['stale'] and r['status'] in {'waiting', 'approval_needed'}]},
                active=[r for r in runs if not r['stale'] and r['status'] in {'running', 'waiting', 'approval_needed'}],
                stale_runs=[r for r in runs if r['stale'] and r['status'] != 'stopped'],
                next=[a for a in actions if a['status'] == 'accepted'],
                suggestions=[a for a in actions if a['status'] in {'observed', 'proposed'}],
                recent=present([dict(r) for r in db.execute('SELECT * FROM events ORDER BY occurred_at DESC, id LIMIT 50')], actions, stamp),
                analytics=traffic_summary(analytics_events))

        from .report_suggestions import view
        result['report_suggestions'] = view(self, 'dashboard', result)
        return result
