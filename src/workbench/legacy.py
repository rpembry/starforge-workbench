"""Transactional, operator-only historical import. Never overwrites live intent."""
import hashlib
import json
import sqlite3
import uuid

from .models import now
from .repository import Problem


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(data):
    return hashlib.sha256(canonical(data).encode()).hexdigest()


def apply_batch(repo, data, principal):
    try:
        return _apply_batch(repo, data, principal)
    except sqlite3.IntegrityError:
        raise Problem(409, 'import_constraint_conflict', 'Import conflicts with existing records; no rows were committed') from None


def _apply_batch(repo, data, principal):
    manifest = digest(data)
    with repo.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT * FROM import_batches WHERE dataset=? AND snapshot_sha256=? AND phase=?',
                         (data['dataset'], data['snapshot_sha256'], data['phase'])).fetchone()
        if old:
            if old['manifest_sha256'] != manifest:
                raise Problem(409, 'import_conflict', 'Snapshot phase already imported with different content')
            return dict(old)
        batch = dict(id=str(uuid.uuid4()), dataset=data['dataset'], snapshot_sha256=data['snapshot_sha256'],
                     phase=data['phase'], manifest_sha256=manifest, record_count=len(data['records']), created_at=now(), recorded_by=principal)
        repo.insert(db, 'import_batches', batch)
        seen = set()
        for row in data['records']:
            if row['legacy_id'] in seen:
                raise Problem(422, 'duplicate_legacy_id', 'Duplicate legacy row in batch')
            seen.add(row['legacy_id'])
            if (data['phase'] == 'events' and (not row['event'] or row['action'])) or (data['phase'] == 'tasks' and (not row['action'] or row['event'])):
                raise Problem(422, 'invalid_import_record', 'Record must match its phase')
            payload = row['event'] or row['action']
            prior = db.execute('SELECT * FROM import_records WHERE dataset=? AND legacy_table=? AND legacy_id=?',
                               (data['dataset'], data['phase'], row['legacy_id'])).fetchone()
            if prior:
                if any(prior[k] != row[k] for k in ['row_sha256', 'source_sha256', 'disposition', 'metadata']) or prior['payload'] != canonical(payload):
                    raise Problem(409, 'legacy_revision_conflict', 'Changed legacy row requires explicit reconciliation; no live record was overwritten')
                record_id = prior['id']
            else:
                record_id = str(uuid.uuid4())
                target_resource = target_id = None
                if row['disposition'] != 'quarantined':
                    target_resource = 'events' if data['phase'] == 'events' else 'actions'
                    observed = db.execute("SELECT id FROM events WHERE source='starforge:codex-observer' AND source_id=?", (row['source_sha256'],)).fetchone() if target_resource == 'events' else None
                    target_id = observed['id'] if observed else str(uuid.uuid4())
                    item = dict(payload, id=target_id, source='legacy:'+data['dataset']+':'+data['phase'], source_id=str(row['legacy_id']))
                    if target_resource == 'events':
                        if not item['occurred_at']:
                            raise Problem(422, 'legacy_timestamp_required', 'Historical event needs its original time')
                        item.update(recorded_at=now(), recorded_by=principal)
                    else:
                        # Preserve explicitly sourced old commitments, not inferred proposals.
                        if item['status'] not in {'accepted', 'in_progress', 'done', 'canceled'}:
                            raise Problem(422, 'invalid_legacy_status', 'Imported commitment requires a supported legacy state')
                        item.update(created_at=now(), updated_at=now(), version=1)
                    if not observed:
                        repo.insert(db, target_resource, item)
                repo.insert(db, 'import_records', dict(id=record_id, dataset=data['dataset'], legacy_table=data['phase'],
                    legacy_id=row['legacy_id'], row_sha256=row['row_sha256'], source_sha256=row['source_sha256'],
                    disposition=row['disposition'], target_resource=target_resource, target_id=target_id,
                    payload=canonical(payload), metadata=row['metadata']))
            repo.insert(db, 'import_batch_records', dict(batch_id=batch['id'], record_id=record_id))
        db.commit()
        return batch
